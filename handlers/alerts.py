"""
알림 핸들러 — /alert /alerts /unalert 정형 + /알림 자연어(Gemini + 버튼).

정형 명령은 above/below/pct_up/pct_down 4종만 인자로 받는다(빠른 입력 위주).
복합 타입(52w_high/52w_low/volume_spike/rsi_above/rsi_below)은 자연어
/알림 으로 등록(파싱 후 [✅등록][❌취소] 버튼 확인).
"""
import time
import uuid
import logging
import asyncio
from datetime import datetime
from telethon import events, Button
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from storage import (
    load_alerts, add_alert, remove_alert_by_id, resolve_ticker_input,
)
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.alerts import check_alerts
from services.gemini import parse_alert_intent

log = logging.getLogger(__name__)

_USAGE = (
    "사용법:\n"
    "  /alert <종목> above <가격> [메모]\n"
    "  /alert <종목> below <가격> [메모]\n"
    "  /alert <종목> +<숫자>% [메모]   (전일 종가 대비 급등)\n"
    "  /alert <종목> -<숫자>% [메모]   (전일 종가 대비 급락)\n\n"
    "예) `/alert 삼성전자 above 90000 익절가`\n"
    "     `/alert NVDA +5%`\n"
    "     `/alert 005930.KS below 75000 손절가`"
)

_OP_DESC = {
    'above':        lambda v: f"≥ {v:,.2f}",
    'below':        lambda v: f"≤ {v:,.2f}",
    'pct_up':       lambda v: f"전일 +{v}% 이상",
    'pct_down':     lambda v: f"전일 -{v}% 이하",
    '52w_high':     lambda v: "52주 신고가 도달",
    '52w_low':      lambda v: "52주 신저가 도달",
    'volume_spike': lambda v: f"거래량 20일 평균 × {v}배 이상",
    'rsi_above':    lambda v: f"RSI(14) > {v}",
    'rsi_below':    lambda v: f"RSI(14) < {v}",
}

_VALUE_REQUIRED = {'above', 'below', 'pct_up', 'pct_down', 'volume_spike', 'rsi_above', 'rsi_below'}


def _parse_alert_args(args_text: str):
    """반환: (ok, (name, type, value, note)) 또는 (False, error_msg)."""
    parts = args_text.strip().split()
    if len(parts) < 2:
        return False, _USAGE

    op_idx = op_type = op_value = None

    for i, tok in enumerate(parts):
        lo = tok.lower()
        if lo in ('above', 'over', '돌파', '넘으면', '위'):
            op_idx, op_type = i, 'above'
            break
        if lo in ('below', 'under', '하회', '내려가면', '아래', '미만'):
            op_idx, op_type = i, 'below'
            break
        if tok.startswith('+') and tok.endswith('%'):
            try:
                op_value = float(tok[1:-1])
                op_idx, op_type = i, 'pct_up'
                break
            except ValueError:
                pass
        if tok.startswith('-') and tok.endswith('%'):
            try:
                op_value = float(tok[1:-1])
                op_idx, op_type = i, 'pct_down'
                break
            except ValueError:
                pass

    if op_idx is None or op_idx == 0:
        return False, _USAGE

    name = " ".join(parts[:op_idx])

    if op_type in ('above', 'below'):
        if op_idx + 1 >= len(parts):
            return False, "임계 가격이 필요합니다.\n\n" + _USAGE
        try:
            op_value = float(parts[op_idx + 1].replace(',', ''))
        except ValueError:
            return False, "가격은 숫자여야 합니다.\n\n" + _USAGE
        note = " ".join(parts[op_idx + 2:])
    else:
        note = " ".join(parts[op_idx + 1:])

    if op_value <= 0:
        return False, "값은 0보다 커야 합니다."

    return True, (name, op_type, op_value, note)


async def handle_alert_add(event, args_text: str):
    """`/alert <종목> <조건> [메모]` — 신규 알림 등록."""
    ok, parsed = _parse_alert_args(args_text)
    if not ok:
        await event.reply(parsed)
        return
    name, op_type, value, note = parsed
    ticker, market, display = resolve_ticker_input(name, get_kr_ticker, US_STOCK_MAP)
    if not ticker:
        await event.reply(
            f"❌ '{name}' 종목을 인식할 수 없습니다.\n"
            "yfinance ticker(예: `005930.KS`, `NVDA`)를 직접 입력해도 됩니다."
        )
        return

    alert = {
        'id':         uuid.uuid4().hex[:8],
        'ticker':     ticker,
        'name':       display,
        'market':     market,
        'type':       op_type,
        'value':      value,
        'note':       note,
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    _, total = add_alert(alert)

    note_line = f"\n- 메모: {note}" if note else ""
    await event.reply(
        f"🔔 **알림 등록**\n\n"
        f"- 종목: **{display}** (`{ticker}`) [{market}]\n"
        f"- 조건: {_OP_DESC[op_type](value)}"
        f"{note_line}\n"
        f"- ID: `{alert['id']}`\n\n"
        f"매 5분 자동 체크. 트리거 시 알림 후 자동 삭제됩니다. (총 활성 {total}개)\n"
        f"/alerts 목록 보기  •  /unalert {alert['id']} 삭제"
    )


async def handle_alert_list(event):
    """`/alerts` — 활성 알림 전체 목록."""
    alerts = load_alerts()
    if not alerts:
        await event.reply(
            "🔔 활성 알림이 없습니다.\n\n"
            "`/alert <종목> above <가격>` 으로 등록하세요."
        )
        return
    lines = [f"🔔 **활성 알림 ({len(alerts)}개)**", ""]
    for a in alerts:
        op = _OP_DESC.get(a['type'], lambda v: str(v))(a['value'])
        note = f"  _({a['note']})_" if a.get('note') else ''
        lines.append(f"• `{a['id']}` **{a['name']}** [{a['market']}]  {op}{note}")
    lines.append("")
    lines.append("/unalert <id>  →  삭제")
    await event.reply("\n".join(lines))


async def handle_alert_remove(event, args_text: str):
    """`/unalert <id>` — 단일 알림 삭제."""
    target = args_text.strip()
    if not target:
        await event.reply("사용법: `/unalert <id>`  (id 는 /alerts 에서 확인)")
        return
    _, msg = remove_alert_by_id(target)
    await event.reply(msg)


async def check_and_notify_alerts():
    """스케줄러에서 5분마다 호출. 트리거 알림을 본인에게 발송."""
    loop = asyncio.get_running_loop()
    try:
        triggered = await loop.run_in_executor(_executor, check_alerts)
    except Exception as e:
        log.warning("알림 체크 실패: %s", e)
        return
    if not triggered:
        return

    for a in triggered:
        ind    = a.get('indicators', {})
        cur    = ind.get('cur', 0)
        prev   = ind.get('prev')
        unit   = '$' if a['market'] == 'US' else '원'
        op_desc = _OP_DESC.get(a['type'], lambda v: str(v))(a['value'])

        detail_lines: list[str] = []
        if prev:
            pct = (cur / prev - 1) * 100
            sign = '+' if pct >= 0 else ''
            detail_lines.append(f"- 전일 종가: {prev:,.2f}{unit} ({sign}{pct:.2f}%)")
        if a['type'] in ('52w_high', '52w_low'):
            if ind.get('high_52w') is not None:
                detail_lines.append(f"- 52주 고가: {ind['high_52w']:,.2f}{unit}")
            if ind.get('low_52w') is not None:
                detail_lines.append(f"- 52주 저가: {ind['low_52w']:,.2f}{unit}")
        if a['type'] == 'volume_spike':
            cv, av = ind.get('cur_volume'), ind.get('avg_volume_20')
            if cv is not None and av and av > 0:
                detail_lines.append(f"- 현재 거래량: {cv:,.0f}")
                detail_lines.append(f"- 20일 평균: {av:,.0f}  (현재가 {cv / av:.1f}배)")
        if a['type'] in ('rsi_above', 'rsi_below') and ind.get('rsi_14') is not None:
            detail_lines.append(f"- RSI(14): {ind['rsi_14']:.1f}")

        details_block = ('\n' + '\n'.join(detail_lines)) if detail_lines else ''
        note_line = f"\n- 메모: {a['note']}" if a.get('note') else ''

        msg = (
            f"🚨 **알림 트리거**\n\n"
            f"- 종목: **{a['name']}** (`{a['ticker']}`) [{a['market']}]\n"
            f"- 현재가: {cur:,.2f}{unit}\n"
            f"- 조건: {op_desc}"
            f"{details_block}"
            f"{note_line}\n\n"
            f"_(1회성 — 자동 삭제됨)_"
        )
        try:
            await bot_client.send_message(MY_TELEGRAM_ID, msg, parse_mode='md')
        except Exception as e:
            log.warning("alert 발송 실패 (%s): %s", a.get('id'), e)


# ── 자연어 /알림 + Inline Button ─────────────────────────────────────────────

_PENDING_ALERTS: dict[str, dict] = {}
_PENDING_TTL_SEC = 300


def _cleanup_pending_alerts():
    now = time.time()
    expired = [k for k, v in _PENDING_ALERTS.items() if now - v.get('_ts', 0) > _PENDING_TTL_SEC]
    for k in expired:
        _PENDING_ALERTS.pop(k, None)


async def handle_alert_natural(event, args_text: str):
    """`/알림 <자연어>` — Gemini 파싱 후 등록 확인 버튼."""
    if not args_text:
        await event.reply(
            "사용법: `/알림 <자연어>`\n"
            "예) `/알림 삼성전자 9만원 넘으면`\n"
            "     `/알림 nvda 10% 빠지면 손절`\n"
            "     `/알림 삼전 52주 신고가`\n"
            "     `/알림 카카오 거래량 5배 터지면`\n"
            "     `/알림 삼성전자 과매도`"
        )
        return

    notice = await event.reply("🤖 알림 의도 파악 중...")
    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(_executor, parse_alert_intent, args_text)

    if not parsed:
        await notice.edit(
            "❌ 알림 의도를 파악하지 못했습니다.\n"
            "예) `/알림 삼성전자 9만원 넘으면`"
        )
        return

    name = parsed.get('name') or ''
    gemini_ticker = (parsed.get('ticker') or '').strip()
    gemini_market = parsed.get('market')

    if gemini_ticker:
        ticker = gemini_ticker.upper() if gemini_market == 'US' else gemini_ticker
        if gemini_market in ('KR', 'US'):
            market = gemini_market
        elif ticker.endswith('.KS') or ticker.endswith('.KQ'):
            market = 'KR'
        else:
            market = 'US'
        display = name or ticker
    else:
        ticker, market, display = resolve_ticker_input(name, get_kr_ticker, US_STOCK_MAP)

    if not ticker:
        await notice.edit(f"❌ '{name}' 종목을 인식할 수 없습니다.")
        return

    alert_type = parsed.get('type')
    value      = parsed.get('value')
    note       = parsed.get('note') or ''

    if alert_type not in _OP_DESC:
        await notice.edit(f"❌ 알림 타입을 인식하지 못했습니다 (`{alert_type}`).")
        return
    if alert_type in _VALUE_REQUIRED and (value is None or value <= 0):
        await notice.edit(f"❌ '{alert_type}' 타입은 양수 값이 필요합니다.")
        return

    _cleanup_pending_alerts()
    pending_id = uuid.uuid4().hex[:12]
    _PENDING_ALERTS[pending_id] = {
        'ticker': ticker, 'name': display, 'market': market,
        'type':   alert_type, 'value': value, 'note': note,
        '_ts':    time.time(),
    }

    note_line = f"\n- 메모: {note}" if note else ''
    msg = (
        f"🔔 **알림 등록 확인**\n\n"
        f"- 종목: **{display}** (`{ticker}`) [{market}]\n"
        f"- 조건: {_OP_DESC[alert_type](value)}"
        f"{note_line}\n\n"
        "매 5분 자동 체크. 트리거 후 자동 삭제(1회성)."
    )
    await notice.edit(
        msg,
        buttons=[[
            Button.inline("✅ 등록", f"alert_confirm:{pending_id}".encode()),
            Button.inline("❌ 취소", f"alert_cancel:{pending_id}".encode()),
        ]],
    )


@bot_client.on(events.CallbackQuery(pattern=rb'^alert_(confirm|cancel):'))
async def on_alert_callback(event):
    """알림 등록 inline button 콜백. 본인만 응답."""
    if event.sender_id != MY_TELEGRAM_ID:
        await event.answer("권한 없음")
        return

    data = event.data.decode()
    is_confirm = data.startswith('alert_confirm:')
    pending_id = data.split(':', 1)[1]
    pending = _PENDING_ALERTS.pop(pending_id, None)

    if pending is None:
        await event.answer("⏰ 만료된 등록 요청", alert=True)
        try:
            await event.edit("⏰ 만료된 알림 등록 (5분 timeout)")
        except Exception as e:
            log.debug("edit 실패: %s", e)
        return

    if not is_confirm:
        await event.answer("취소됨")
        try:
            await event.edit("❌ 취소되었습니다.")
        except Exception as e:
            log.debug("edit 실패: %s", e)
        return

    alert = {
        'id':         uuid.uuid4().hex[:8],
        'ticker':     pending['ticker'],
        'name':       pending['name'],
        'market':     pending['market'],
        'type':       pending['type'],
        'value':      pending['value'],
        'note':       pending.get('note', ''),
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    _, total = add_alert(alert)

    await event.answer("등록 완료")
    try:
        await event.edit(
            f"✅ 알림 등록 (ID `{alert['id']}`, 활성 {total}개)\n\n"
            f"/alerts 목록  •  /unalert {alert['id']} 삭제"
        )
    except Exception as e:
        log.debug("edit 실패: %s", e)
