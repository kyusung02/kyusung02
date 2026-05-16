"""
어닝 핸들러 — /earnings <종목> 명령 + 매일 D-1 푸시 알림.

조회: 종목 식별 → yfinance Ticker.earnings_dates → 메시지 포맷.
푸시: 매일 08:30 KST 스케줄 → 보유+관심 미국 종목 중 D-0/D-1 추출 →
      "ticker:date" 키로 중복 발송 방지.
"""
import logging
import asyncio
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from storage import (
    resolve_ticker_input, load_portfolio, load_watchlist,
    load_seen_earnings, save_seen_earnings,
)
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.earnings import (
    get_earnings_history, format_earnings_message,
    collect_imminent_earnings,
)

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


def _collect_us_tickers_for_alert() -> dict[str, str]:
    """보유 + 관심 종목 중 미국 종목만 추출. ticker → 표시 이름 매핑 반환."""
    name_map: dict[str, str] = {}
    for tk, data in (load_portfolio() or {}).items():
        if data.get('market') == 'US':
            name_map[tk] = data.get('name', tk)
    for nm in load_watchlist():
        tk, mk, disp = resolve_ticker_input(nm, get_kr_ticker, US_STOCK_MAP)
        if tk and mk == 'US' and tk not in name_map:
            name_map[tk] = disp
    return name_map


async def check_and_notify_imminent_earnings():
    """스케줄러용 — D-0/D-1 어닝을 본인에게 푸시. 같은 (ticker,date) 키는 1회만 발송."""
    name_map = _collect_us_tickers_for_alert()
    if not name_map:
        return

    loop = asyncio.get_running_loop()
    try:
        upcoming = await loop.run_in_executor(
            _executor, collect_imminent_earnings, list(name_map.keys()), 1,
        )
    except Exception as e:
        log.warning("D-1 어닝 체크 실패: %s", e)
        return
    if not upcoming:
        return

    seen = load_seen_earnings()
    fresh: list[dict] = []
    for u in upcoming:
        key = f"{u['ticker']}:{u['date']}"
        if key in seen:
            continue
        fresh.append(u)
        seen.add(key)

    if not fresh:
        return

    for u in fresh:
        marker = "🔥 오늘" if u['days_until'] == 0 else f"D-{u['days_until']}"
        est_line = f"\n- 컨센서스 EPS: ${u['eps_est']:.2f}" if u.get('eps_est') is not None else ""
        msg = (
            f"📣 **어닝 임박 — {marker}**\n\n"
            f"- 종목: **{name_map[u['ticker']]}** (`{u['ticker']}`)\n"
            f"- 발표일: {u['date']}"
            f"{est_line}\n\n"
            f"_(발표 후 `/어닝 {u['ticker']}` 로 결과 + 서프라이즈% 확인)_"
        )
        try:
            await bot_client.send_message(MY_TELEGRAM_ID, msg, parse_mode='md')
        except Exception as e:
            log.warning("D-1 알림 발송 실패 (%s): %s", u['ticker'], e)

    save_seen_earnings(seen)
