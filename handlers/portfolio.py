"""
포트폴리오 핸들러 — /buy /sell /portfolio 명령.

명령 파싱 → 종목 식별 → storage 위임 → 응답.
평가는 services.portfolio.evaluate_portfolio 에 위임 (yfinance 호출 → executor).
"""
import re
import logging
import asyncio
from datetime import date
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from storage import (
    load_portfolio, add_buy, add_sell, resolve_ticker_input,
)
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.portfolio import evaluate_portfolio, format_portfolio_message

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
