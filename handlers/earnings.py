"""
어닝 핸들러 — /earnings <종목> 명령.

종목 식별 → yfinance Ticker.earnings_dates → 메시지 포맷.
"""
import logging
import asyncio
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from storage import resolve_ticker_input
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.earnings import get_earnings_history, format_earnings_message

log = logging.getLogger(__name__)


async def handle_earnings(event, args_text: str):
    """`/earnings <종목>` — 어닝 히스토리 + 다음 예정."""
    name = args_text.strip()
    if not name:
        await event.reply(
            "사용법: `/earnings <종목>` (또는 `/어닝 <종목>`)\n"
            "예) `/earnings NVDA`, `/어닝 엔비디아`, `/어닝 TSLA`\n\n"
            "※ 미국 종목 위주. 국내 종목은 데이터 미지원."
        )
        return

    ticker, market, display = resolve_ticker_input(name, get_kr_ticker, US_STOCK_MAP)
    if not ticker:
        await event.reply(f"❌ '{name}' 종목을 인식할 수 없습니다.")
        return

    if market == 'KR':
        await event.reply(
            f"⚠️ 국내 종목({display})은 yfinance 어닝 데이터가 거의 비어 있습니다.\n"
            "다음 단계에서 DART 정기보고서 + 컨센서스 스크래핑으로 지원 예정."
        )
        return

    await event.reply(f"📅 **{display}** 어닝 데이터 조회 중...")
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(_executor, get_earnings_history, ticker)
    except Exception as e:
        log.warning("earnings 조회 실패: %s", e)
        await event.reply("⚠️ 어닝 데이터 조회 중 오류가 발생했습니다.")
        return

    if not data:
        await event.reply(f"⚠️ {display} (`{ticker}`)의 어닝 데이터가 없습니다.")
        return

    await bot_client.send_message(
        MY_TELEGRAM_ID,
        format_earnings_message(display, ticker, data),
        parse_mode='md',
    )
