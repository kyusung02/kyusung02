"""
시황 핸들러 — 모닝 시황 브리핑 (미국 3대 지수, 원자재, 금리, BTC)
"""
import logging
import asyncio
from datetime import timedelta
import yfinance as yf
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID, BROADCAST_ID
from services.gemini import generate_with_retry, build_morning_market_prompt
from storage import load_portfolio, collect_us_name_map
from services.portfolio import evaluate_portfolio, format_portfolio_message
from utils import kst_today
from services.stock import get_kr_ticker, US_STOCK_MAP
from services.earnings import get_upcoming_earnings_for, format_upcoming_briefing
from services.kr_index import fetch_naver_kr_index

log = logging.getLogger(__name__)

# 모닝 시황이 조회하는 지표 목록 — 추가/삭제는 여기서만
_MORNING_TICKERS = {
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


def _fetch_us_morning_data() -> str:
    """yfinance로 글로벌 시장 주요 지표 수집 (동기 함수 - executor에서 실행)"""
    lines = []

    # 한국 지수는 직전 거래일 일봉이 yfinance에서 누락되는 경우가 잦다. 갭이 있으면
    # iloc[-2]가 며칠 전 종가를 집어 전일 등락이 다중일 변동률로 둔갑한다(섹터 HPSP와
    # 동일 결함). 두 KR 지수 날짜의 합집합으로 '거래일 달력'을 만들어, 최신 바가
    # stale하거나 직전 거래일 바가 비면 숫자를 꾸며내지 않고 '데이터 지연'으로 표시한다.
    # (장전 07시엔 previous_close가 last와 같아 0%가 되므로 보정엔 못 쓴다 — 플래그가 정직.)
    _KR_SYMS = ('^KS11', '^KQ11')
    kr_hist, kr_days = {}, set()
    for s in _KR_SYMS:
        try:
            h = yf.Ticker(s).history(period='5d')
            kr_hist[s] = h
            kr_days.update(d.date() for d in h.index)
        except Exception as e:
            log.warning("yfinance(%s) 조회 실패: %s", s, e)
            kr_hist[s] = None
    kr_calendar = sorted(kr_days)

    # 절대 staleness 기준 = KST 오늘 직전의 '평일'(직전 거래일 근사). 위 ^KS11↔^KQ11
    # 교차검증은 두 지수가 '똑같이' 한 거래일씩 묵으면(yfinance가 양쪽 최신 일봉을 동시
    # 누락) 둘이 서로 일치해버려 못 잡는다 — 2026-06-24 사고: 06-23 폭락(-10%)이 06-22
    # 종가(+0.7%)로 '전일' 둔갑. 최신 바가 이 직전 평일보다도 과거면 세션 하나가 통째로
    # 빠진 것이므로 숫자를 '전일'로 단언하지 않고 지연으로 표시한다. (평일 공휴일 다음날엔
    # 과대검출 가능하나, stale 등락을 '전일'로 내보내는 것보다 안전한 실패 방향이다.)
    prev_weekday = kst_today() - timedelta(days=1)
    while prev_weekday.weekday() >= 5:      # 5=토, 6=일 → 직전 평일까지 후퇴
        prev_weekday -= timedelta(days=1)

    # yfinance가 KR 지수를 못 줄 때(지연/데이터부족/예외)만 네이버로 폴백한 한 줄을 만든다.
    # 07시 장전엔 yfinance에 전일 일봉이 아직 없어 '전일 데이터 지연'으로 막히는 일이 잦은데,
    # 네이버는 마감 직후 전일 종가·등락률을 직접 줘서 그 시각에도 숫자를 보여줄 수 있다.
    # 호출당 HTTP 1회라 실제 폴백이 필요한 KR 심볼에 한해 지연 조회하고 결과를 캐시한다.
    _naver_cache: dict = {}

    def _kr_naver_line(disp_name, yf_sym):
        """네이버 폴백 한 줄(성공 시) 또는 None(실패 시 — 호출부가 기존 안내로 폴백)."""
        if yf_sym not in _naver_cache:
            _naver_cache[yf_sym] = fetch_naver_kr_index(yf_sym)
        nv = _naver_cache[yf_sym]
        if not nv:
            return None
        arrow = '▲' if nv['pct'] >= 0 else '▼'
        sign  = '+' if nv['pct'] >= 0 else ''
        tag   = f" ({nv['date']:%m/%d} 종가·네이버)" if nv['date'] else " (네이버)"
        return f"{disp_name}: {nv['price']:,.2f}  {arrow} {sign}{nv['pct']:.1f}%{tag}"

    for name, sym in _MORNING_TICKERS.items():
        try:
            # period='2d'는 거래일 경계/타임존 때문에 1행만 돌아오는 경우가 잦다
            # (특히 07시 장전 KOSPI/KOSDAQ). 1행이면 prev=cur가 되어 0.0% 보합으로
            # 둔갑하므로, 넉넉히 5d를 받아 마지막 두 '행'으로 등락을 계산한다.
            hist = kr_hist[sym] if sym in _KR_SYMS else yf.Ticker(sym).history(period='5d')
            if hist is None or hist.empty or len(hist) < 2:
                if sym in _KR_SYMS and (fb := _kr_naver_line(name, sym)):
                    lines.append(fb)
                    continue
                n = 0 if hist is None else len(hist)
                lines.append(f"{name}: 데이터 부족(거래일 {n}일)")
                continue

            # 한국 지수: 직전 거래일 바 누락(데이터 갭) 검사 — 전일% 왜곡 방지
            if sym in _KR_SYMS and kr_calendar:
                cur_day = hist.index[-1].date()
                priors  = [d for d in kr_calendar if d < cur_day]
                stale   = cur_day != kr_calendar[-1]
                gap     = bool(priors) and hist.index[-2].date() != priors[-1]
                behind  = cur_day < prev_weekday   # 양쪽 동시 누락(교차검증 사각지대) 포착
                if stale or gap or behind:
                    # yfinance 일봉이 지연/누락 → 네이버로 전일 종가를 채운다. 네이버도
                    # 실패하면 숫자를 꾸며내지 않고 기존 '전일 데이터 지연' 안내로 되돌아간다.
                    lines.append(_kr_naver_line(name, sym)
                                 or f"{name}: 전일 데이터 지연(직전 거래일 종가 누락)")
                    continue

            cur   = hist['Close'].iloc[-1]
            prev  = hist['Close'].iloc[-2]
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
            if sym in _KR_SYMS and (fb := _kr_naver_line(name, sym)):
                lines.append(fb)
                continue
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
    name_map   = collect_us_name_map(get_kr_ticker, US_STOCK_MAP, portfolio)
    us_tickers = list(name_map.keys())
    if us_tickers:
        try:
            upcoming = await loop.run_in_executor(_executor, get_upcoming_earnings_for, us_tickers, 7)
            if upcoming:
                msg = format_upcoming_briefing(upcoming, name_map)
                await bot_client.send_message(MY_TELEGRAM_ID, msg, parse_mode='md')
        except Exception as e:
            log.warning("어닝 예정 조회 실패: %s", e)
