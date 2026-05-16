"""
포트폴리오 핸들러 — /buy /sell /portfolio + /거래 (자연어 + inline button).

정형 명령: /buy /sell /portfolio
자유 입력: /거래 <자연어> → Gemini 파싱 → 확인 버튼 → 실행

명령 파싱 → 종목 식별 → storage 위임 → 응답.
평가는 services.portfolio.evaluate_portfolio 에 위임 (yfinance 호출 → executor).
"""
import re
import time
import uuid
import logging
import asyncio
from datetime import date
from telethon import events, Button
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from storage import (
    load_portfolio, add_buy, add_sell, resolve_ticker_input,
)
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.portfolio import evaluate_portfolio, format_portfolio_message
from services.gemini import parse_trade_intent

log = logging.getLogger(__name__)

_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

_USAGE_BUY  = "사용법: /buy 종목명 수량 단가 [매수일(YYYY-MM-DD)]\n예) /buy 삼성전자 10 80000\n     /buy NVDA 5 102.81 2025-12-01"
_USAGE_SELL = "사용법: /sell 종목명 [수량]\n수량 생략 시 전량 매도.\n예) /sell 삼성전자 5\n     /sell NVDA"


def _parse_trade_args(args_text: str, expect_price: bool) -> tuple:
    """매매 명령 파싱.

    expect_price=True (/buy):  반환 (name, shares, price, trade_date|None) 또는 (None, msg)
    expect_price=False (/sell): 반환 (name, shares|None) 또는 (None, msg)
    """
    parts = args_text.strip().split()
    if not parts:
        return None, "종목명이 비어있습니다."

    trade_date = None
    if expect_price and len(parts) >= 4 and _ISO_DATE.match(parts[-1]):
        trade_date = parts[-1]
        parts = parts[:-1]

    if expect_price:
        if len(parts) < 3:
            return None, _USAGE_BUY
        try:
            price = float(parts[-1].replace(',', ''))
            shares = float(parts[-2].replace(',', ''))
        except ValueError:
            return None, "수량·단가는 숫자여야 합니다.\n\n" + _USAGE_BUY
        name = " ".join(parts[:-2])
        return name, shares, price, trade_date

    # /sell
    shares = None
    if len(parts) >= 2:
        try:
            shares = float(parts[-1].replace(',', ''))
            parts = parts[:-1]
        except ValueError:
            pass  # 마지막 토큰이 숫자가 아니면 전량 매도로 간주
    name = " ".join(parts)
    if not name:
        return None, _USAGE_SELL
    return name, shares


async def handle_buy(event, args_text: str):
    """`/buy 종목명 수량 단가 [날짜]` — 매수 기록."""
    parsed = _parse_trade_args(args_text, expect_price=True)
    if parsed[0] is None:
        await event.reply(parsed[1])
        return
    name, shares, price, trade_date = parsed
    ticker, market, display = resolve_ticker_input(name, get_kr_ticker, US_STOCK_MAP)
    if not ticker:
        await event.reply(
            f"❌ '{name}' 종목을 인식할 수 없습니다.\n"
            "국내는 DART 등록 종목명, 미국은 티커(예: NVDA) 또는 한글명(예: 엔비디아) 사용."
        )
        return
    if trade_date is None:
        trade_date = date.today().isoformat()
    ok, msg = add_buy(ticker, display, market, shares, price, trade_date)
    await event.reply(
        f"{msg}\n"
        f"- 티커: `{ticker}` [{market}]\n"
        f"- 매수일: {trade_date}\n\n"
        "/portfolio  →  전체 평가 보기"
    )


async def handle_sell(event, args_text: str):
    """`/sell 종목명 [수량]` — 매도 처리."""
    parsed = _parse_trade_args(args_text, expect_price=False)
    if parsed[0] is None:
        await event.reply(parsed[1])
        return
    name, shares = parsed
    ticker, _, _ = resolve_ticker_input(name, get_kr_ticker, US_STOCK_MAP)
    if not ticker:
        await event.reply(f"❌ '{name}' 종목을 인식할 수 없습니다.")
        return
    ok, msg = add_sell(ticker, shares)
    await event.reply(msg + ("\n\n/portfolio  →  남은 평가 보기" if ok else ""))


async def handle_portfolio(event):
    """`/portfolio` — 전체 평가 (yfinance 호출은 executor에서)."""
    p = load_portfolio()
    if not p:
        await event.reply(
            "💼 포트폴리오가 비어 있습니다.\n\n"
            "/buy 종목명 수량 단가  →  매수 기록\n"
            "예) /buy 삼성전자 10 80000"
        )
        return
    await event.reply(f"💼 포트폴리오 평가 중... ({len(p)}종목 가격 조회)")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(_executor, evaluate_portfolio, p)
    except Exception as e:
        log.warning("포트폴리오 평가 실패: %s", e)
        await event.reply("⚠️ 포트폴리오 평가 중 오류가 발생했습니다.")
        return
    await bot_client.send_message(MY_TELEGRAM_ID, format_portfolio_message(result), parse_mode='md')


# ── 자연어 거래 (/거래) + Inline Button 확인 ──────────────────────────────────
# 봇 메모리에만 보관 (재시작 시 사라짐, 5분 timeout). 영속화 불필요.

_PENDING_TRADES: dict[str, dict] = {}
_PENDING_TTL_SEC = 300


def _cleanup_pending():
    now = time.time()
    expired = [k for k, v in _PENDING_TRADES.items() if now - v.get('_ts', 0) > _PENDING_TTL_SEC]
    for k in expired:
        _PENDING_TRADES.pop(k, None)


def _format_pending_summary(p: dict) -> str:
    action  = p['action']
    display = p['display']
    ticker  = p['ticker']
    market  = p['market']
    unit    = '$' if market == 'US' else '원'

    head = f"📋 **매매 의도 확인**\n\n- 종목: **{display}** (`{ticker}`) [{market}]"

    if action == 'buy':
        shares = p['shares']
        price  = p['price']
        total  = shares * price
        return (
            f"{head}\n"
            f"- 행위: **매수**\n"
            f"- 수량: {shares}주\n"
            f"- 단가: {price:,.2f}{unit}\n"
            f"- 합계: {total:,.2f}{unit}\n"
            f"- 매수일: {p['trade_date']}"
        )
    sold = '전량' if p.get('shares') is None else f"{p['shares']}주"
    return f"{head}\n- 행위: **매도**\n- 수량: {sold}"


async def handle_trade(event, args_text: str):
    """`/거래 <자연어>` — Gemini 파싱 → 확인 버튼 노출."""
    if not args_text:
        await event.reply(
            "사용법: `/거래 <자연어>`\n"
            "예) `/거래 삼성전자 10주 8만원에 샀어`\n"
            "     `/거래 엔비디아 5주 102.81달러 12월 1일`\n"
            "     `/거래 삼전 3주 매도`\n"
            "     `/거래 nvda 전량 정리`"
        )
        return

    notice = await event.reply("🤖 의도 파악 중...")
    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(_executor, parse_trade_intent, args_text)

    if not parsed:
        await notice.edit(
            "❌ 매매 의도를 파악하지 못했습니다.\n"
            "더 명확하게 입력해주세요. 예) `/거래 삼성전자 10주 8만원에 매수`"
        )
        return

    name = parsed.get('name') or ''
    ticker, market, display = resolve_ticker_input(name, get_kr_ticker, US_STOCK_MAP)
    if not ticker:
        await notice.edit(f"❌ '{name}' 종목을 인식할 수 없습니다.")
        return

    parsed.update({'ticker': ticker, 'market': market, 'display': display, '_ts': time.time()})

    if parsed['action'] == 'buy':
        if not parsed.get('shares') or not parsed.get('price'):
            await notice.edit("⚠️ 매수는 수량과 단가가 모두 필요합니다.")
            return
        parsed['trade_date'] = parsed.get('trade_date') or date.today().isoformat()

    _cleanup_pending()
    trade_id = uuid.uuid4().hex[:12]
    _PENDING_TRADES[trade_id] = parsed

    await notice.edit(
        _format_pending_summary(parsed),
        buttons=[[
            Button.inline("✅ 실행", f"trade_confirm:{trade_id}".encode()),
            Button.inline("❌ 취소", f"trade_cancel:{trade_id}".encode()),
        ]],
    )


@bot_client.on(events.CallbackQuery(pattern=rb'^trade_(confirm|cancel):'))
async def on_trade_callback(event):
    """Inline button 콜백. 본인 계정만 응답."""
    if event.sender_id != MY_TELEGRAM_ID:
        await event.answer("권한 없음")
        return

    data = event.data.decode()
    is_confirm = data.startswith('trade_confirm:')
    trade_id = data.split(':', 1)[1]
    pending = _PENDING_TRADES.pop(trade_id, None)

    if pending is None:
        await event.answer("⏰ 만료된 거래입니다", alert=True)
        try:
            await event.edit("⏰ 만료된 거래 (5분 timeout)")
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

    if pending['action'] == 'buy':
        ok, msg = add_buy(
            pending['ticker'], pending['display'], pending['market'],
            pending['shares'], pending['price'], pending['trade_date'],
        )
    else:
        ok, msg = add_sell(pending['ticker'], pending.get('shares'))

    await event.answer("실행 완료" if ok else "실패", alert=not ok)
    try:
        await event.edit(("✅ " if ok else "❌ ") + msg + "\n\n/portfolio  →  현황 보기")
    except Exception as e:
        log.debug("edit 실패: %s", e)
