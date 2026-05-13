"""
시황 핸들러 — 모닝 시황 브리핑 (미국 3대 지수, 원자재, 금리, BTC)
"""
import logging
import asyncio
from datetime import date
import yfinance as yf
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from services.gemini import generate_with_retry

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
    """매일 07:00 KST 자동 실행되는 미국 시황 브리핑 함수입니다."""
    loop     = asyncio.get_running_loop()
    data_str = await loop.run_in_executor(_executor, _fetch_us_morning_data)

    today  = date.today().strftime('%Y년 %m월 %d일')
    prompt = (
        f"당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.\n"
        f"아래는 {today} 기준 글로벌 시장 주요 지표입니다.\n\n"
        f"{data_str}\n\n"
        f"아래 형식으로 간결하게 정리하세요.\n\n"
        f"[🌅 모닝 시황 브리핑 - {today}]\n\n"
        f"■ 미국 3대 지수 흐름\n"
        f"- S&P500 / NASDAQ / DOW 등락 요약 및 전일 대비 의미\n\n"
        f"■ 국내 시장 전망\n"
        f"- KOSPI / KOSDAQ 오늘 시가 방향 예상 (미국 흐름 반영)\n\n"
        f"■ 원자재 & 금리\n"
        f"- 금, WTI, 미국 2Y/10Y 금리 동향 및 투자 시사점\n\n"
        f"■ 리스크 & 기회\n"
        f"- 오늘 주목할 매크로 변수 또는 이벤트 1~2가지\n\n"
        f"3줄 이내로 각 섹션을 간결하게 서술하세요."
    )

    try:
        response = await loop.run_in_executor(_executor, generate_with_retry, [prompt])
    except Exception as e:
        log.warning("Gemini 시황 요약 실패: %s", e)
        await bot_client.send_message(MY_TELEGRAM_ID,
            f"⚠️ 시황 분석 중 오류가 발생했습니다.\n\n📌 **원본 데이터**\n```\n{data_str}\n```"
        )
        return

    raw_data_block = f"\n\n📌 **원본 데이터**\n```\n{data_str}\n```"
    await bot_client.send_message(MY_TELEGRAM_ID, response.text + raw_data_block)
