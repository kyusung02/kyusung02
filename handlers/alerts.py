"""
알림 핸들러 — /alert /alerts /unalert 정형 명령.

명령 파싱 → 종목 식별 → storage 위임 → 응답.
스케줄러에서 호출되는 check_and_notify_alerts 는 services.alerts.check_alerts
결과를 메시지로 변환해 본인에게 발송.
"""
import uuid
import logging
import asyncio
from datetime import datetime
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from storage import (
    load_alerts, add_alert, remove_alert_by_id, resolve_ticker_input,
)
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.alerts import check_alerts

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
    'above':    lambda v: f"≥ {v:,.2f}",
    'below':    lambda v: f"≤ {v:,.2f}",
    'pct_up':   lambda v: f"전일 +{v}% 이상",
    'pct_down': lambda v: f"전일 -{v}% 이하",
}


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
        cur    = a.get('triggered_price', 0)
        prev   = a.get('prev_close')
        unit   = '$' if a['market'] == 'US' else '원'
        op_desc = _OP_DESC.get(a['type'], lambda v: str(v))(a['value'])

        pct_line = ''
        if prev:
            pct = (cur / prev - 1) * 100
            sign = '+' if pct >= 0 else ''
            pct_line = f"\n- 전일 종가: {prev:,.2f}{unit} ({sign}{pct:.2f}%)"

        note_line = f"\n- 메모: {a['note']}" if a.get('note') else ''

        msg = (
            f"🚨 **알림 트리거**\n\n"
            f"- 종목: **{a['name']}** (`{a['ticker']}`) [{a['market']}]\n"
            f"- 현재가: {cur:,.2f}{unit}\n"
            f"- 조건: {op_desc}"
            f"{pct_line}"
            f"{note_line}\n\n"
            f"_(1회성 — 자동 삭제됨)_"
        )
        try:
            await bot_client.send_message(MY_TELEGRAM_ID, msg, parse_mode='md')
        except Exception as e:
            log.warning("alert 발송 실패 (%s): %s", a.get('id'), e)
