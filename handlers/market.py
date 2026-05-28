"""
시황 핸들러 — 모닝 시황 브리핑 (미국 3대 지수, 원자재, 금리, BTC)
"""
import logging
import asyncio
import yfinance as yf
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID, BROADCAST_ID
from services.gemini import generate_with_retry, build_morning_market_prompt
from storage import load_portfolio, load_watchlist, resolve_ticker_input
from services.portfolio import evaluate_portfolio, format_portfolio_message
from utils import kst_today
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.earnings import get_upcoming_earnings_for, format_upcoming_briefing

log = logging.getLogger(__name__)


def _fetch_us_morning_data() -> str:
    """yfinance로 글로벌 시장 주요 지표 수집 (동기 함수 - executor에서 실행)"""
    TICKERS = {
        'S&P 500':  '^GSPC',
        'NASDAQ':   '^IXIC',
        'DOW':      '^DJI',
        'KOSPI':    '^KS11',
        'KOSDAQ':   '^KQ11',
        '금(Gold)': 'GC=F',
        'WTI':      'CL=F',
        '미국 2Y':  '^IRX',
        '미국 10Y': '^TNX',
        'VIX':      '^VIX',
        'BTC':      'BTC-USD',
        '필라델피아 SOX': '^SOX',
        '반도체 ETF SMH': 'SMH',
    }

    lines = []
    for name, sym in TICKERS.items():
        try:
            hist = yf.Ticker(sym).history(period='2d')
            if hist.empty or len(hist) < 1:
                lines.append(f"{name}: 데이터 없음")
                continue
            cur   = hist['Close'].iloc[-1]
            prev  = hist['Close'].iloc[-2] if len(hist) >= 2 else cur
            chg   = cur - prev
            pct   = chg / prev * 100 if prev else 0
            arrow = '▲' if chg >= 0 else '▼'
            sign  = '+' if chg >= 0 else ''

            if sym in ('^IRX', '^TNX', '^VIX'):
                lines.append(f"{name}: {cur:.2f}  {arrow} {sign}{pct:.1f}%")
            elif sym == 'BTC-USD':
                lines.append(f"{name}: ${cur:,.0f}  {arrow} {sign}{pct:.1f}%")
            elif sym in ('GC=F', 'CL=F'):
                lines.append(f"{name}: ${cur:,.1f}  {arrow} {sign}{pct:.1f}%")
            elif sym in ('^KS11', '^KQ11'):
                lines.append(f"{name}: {cur:,.2f}  {arrow} {sign}{pct:.1f}%")
            else:
                lines.append(f"{name}: {cur:,.2f}  {arrow} {sign}{pct:.1f}%")
        except Exception as e:
            log.warning("yfinance(%s) 조회 실패: %s", sym, e)
            lines.append(f"{name}: 조회 실패")

    return '\n'.join(lines)


async def send_us_morning():
    """매일 07:00 KST 자동 실행되는 미국 시황 브리핑.

    포트폴리오가 등록되어 있으면 평가도 별도 메시지로 자동 첨부.
    평가 실패는 시황 발송 자체에는 영향을 주지 않는다.
    """
    loop     = asyncio.get_running_loop()
    data_str = await loop.run_in_executor(_executor, _fetch_us_morning_data)

    today  = kst_today().strftime('%Y년 %m월 %d일')
    prompt = build_morning_market_prompt(data_str, today)

    try:
        response = await loop.run_in_executor(_executor, generate_with_retry, [prompt])
    except Exception as e:
        log.warning("Gemini 시황 요약 실패: %s", e)
        await bot_client.send_message(BROADCAST_ID,
            f"⚠️ 시황 분석 중 오류가 발생했습니다.\n\n📌 **원본 데이터**\n```\n{data_str}\n```"
        )
    else:
        raw_data_block = f"\n\n📌 **원본 데이터**\n```\n{data_str}\n```"
        await bot_client.send_message(BROADCAST_ID, response.text + raw_data_block)

    # 포트폴리오 자동 평가 첨부 (보유 종목이 있을 때만)
    portfolio = load_portfolio()
    if portfolio:
        try:
            result = await loop.run_in_executor(_executor, evaluate_portfolio, portfolio)
            await bot_client.send_message(
                MY_TELEGRAM_ID, format_portfolio_message(result), parse_mode='md',
            )
        except Exception as e:
            log.warning("모닝 포트폴리오 평가 실패: %s", e)

    # 1주 내 어닝 예정 자동 첨부 (보유+관심 종목, 미국만 — yfinance 데이터 한계)
    us_tickers, name_map = _collect_us_tickers_for_earnings(portfolio)
    if us_tickers:
        try:
            upcoming = await loop.run_in_executor(_executor, get_upcoming_earnings_for, us_tickers, 7)
            if upcoming:
                msg = format_upcoming_briefing(upcoming, name_map)
                await bot_client.send_message(MY_TELEGRAM_ID, msg, parse_mode='md')
        except Exception as e:
            log.warning("어닝 예정 조회 실패: %s", e)


def _collect_us_tickers_for_earnings(portfolio: dict) -> tuple[list[str], dict[str, str]]:
    """모닝 시황 첨부용 — 보유+관심 종목 중 미국만 추출.

    국내 종목은 yfinance earnings_dates 가 거의 비어 있어 제외.
    portfolio 는 이미 ticker 키. watchlist 는 종목명 → ticker 변환 필요.
    """
    name_map: dict[str, str] = {}

    for tk, data in (portfolio or {}).items():
        if data.get('market') == 'US':
            name_map[tk] = data.get('name', tk)

    for nm in load_watchlist():
        tk, mk, disp = resolve_ticker_input(nm, get_kr_ticker, US_STOCK_MAP)
        if tk and mk == 'US' and tk not in name_map:
            name_map[tk] = disp

    return list(name_map.keys()), name_map
