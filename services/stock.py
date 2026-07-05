"""
주가 데이터 — yfinance 조회, DART 종목 코드 변환, 미국 종목 매핑
"""
import json
import logging
import urllib.request
import yfinance as yf
import OpenDartReader
from config import DART_API_KEY
from utils import safe_opener

log = logging.getLogger(__name__)


def _patch_opendart_timeout(seconds: int = 30) -> None:
    """OpenDartReader 내부 requests 호출에 기본 타임아웃 주입.

    OpenDartReader는 내부 requests.get/post 전부(실측 61곳)에 timeout이 없어,
    DART 서버가 응답을 멈추면 executor 스레드가 무한 대기하며 슬롯(4개)을
    영구 잠식한다. 전역 socket.setdefaulttimeout()은 requests가 내부에서
    명시적 None으로 덮어써 read 단계에 안 먹히는 것을 실측 확인(2026-07-05)
    → 모듈별 requests 참조를 timeout 주입 래퍼로 교체하는 방식 사용.
    dart 인스턴스 생성 전에 호출해야 corp_codes 다운로드부터 보호된다.
    """
    import sys
    import functools
    import requests as _requests

    class _TimeoutRequests:
        def __getattr__(self, name):
            attr = getattr(_requests, name)
            if name in ("get", "post") and callable(attr):
                @functools.wraps(attr)
                def _with_timeout(*args, **kwargs):
                    kwargs.setdefault("timeout", seconds)
                    return attr(*args, **kwargs)
                return _with_timeout
            return attr

    shim = _TimeoutRequests()
    for name, mod in list(sys.modules.items()):
        if name.split(".")[0] == "OpenDartReader" and getattr(mod, "requests", None) is _requests:
            mod.requests = shim


try:
    _patch_opendart_timeout()
except Exception:  # 패치가 실패해도 봇 기동은 계속돼야 한다 (hang 안전망만 빠질 뿐)
    log.exception("OpenDartReader 타임아웃 패치 실패")

dart = OpenDartReader(DART_API_KEY)

# ── 미국 종목 한글/영문 → ticker 매핑 ────────────────────────────────────────
US_STOCK_MAP = {
    # 반도체
    '엔비디아': 'NVDA', 'nvidia': 'NVDA',
    'AMD': 'AMD', 'amd': 'AMD',
    '인텔': 'INTC', 'intel': 'INTC',
    '퀄컴': 'QCOM', 'qualcomm': 'QCOM',
    '마이크론': 'MU', 'micron': 'MU',
    'TSMC': 'TSM', '대만반도체': 'TSM',
    '브로드컴': 'AVGO', 'broadcom': 'AVGO',
    'ARM': 'ARM', 'arm': 'ARM',
    # 빅테크
    '애플': 'AAPL', 'apple': 'AAPL',
    '마이크로소프트': 'MSFT', 'microsoft': 'MSFT',
    '구글': 'GOOGL', '알파벳': 'GOOGL', 'alphabet': 'GOOGL', 'google': 'GOOGL',
    '아마존': 'AMZN', 'amazon': 'AMZN',
    '메타': 'META', 'meta': 'META',
    '넷플릭스': 'NFLX', 'netflix': 'NFLX',
    # EV/전기차
    '테슬라': 'TSLA', 'tesla': 'TSLA',
    # 금융
    '버크셔': 'BRK-B', 'berkshire': 'BRK-B',
    '비자': 'V', 'visa': 'V',
    '마스터카드': 'MA', 'mastercard': 'MA',
    'JP모건': 'JPM', 'jpmorgan': 'JPM',
    # AI/소프트웨어
    '팔란티어': 'PLTR', 'palantir': 'PLTR',
    '스노우플레이크': 'SNOW', 'snowflake': 'SNOW',
    '세일즈포스': 'CRM', 'salesforce': 'CRM',
    # 기타
    '코인베이스': 'COIN', 'coinbase': 'COIN',
    '코카콜라': 'KO', 'cocacola': 'KO',
    '존슨앤존슨': 'JNJ',
    'ASML': 'ASML', 'asml': 'ASML',
}

# 주요 국내 종목 하드코딩 폴백 (DART API 실패 시)
_KR_TICKER_FALLBACK = {
    '삼성전자': '005930.KS', 'SK하이닉스': '000660.KS', 'LG에너지솔루션': '373220.KS',
    '삼성바이오로직스': '207940.KS', '현대차': '005380.KS', '기아': '000270.KS',
    'POSCO홀딩스': '005490.KS', '셀트리온': '068270.KS', '카카오': '035720.KS',
    '네이버': '035420.KS', 'NAVER': '035420.KS', 'LG화학': '051910.KS',
    '삼성SDI': '006400.KS', '현대모비스': '012330.KS', 'KB금융': '105560.KS',
    '신한지주': '055550.KS', '하나금융지주': '086790.KS', '우리금융지주': '316140.KS',
    'KT&G': '033780.KS', 'SK텔레콤': '017670.KS', 'KT': '030200.KS',
    'LG전자': '066570.KS', '롯데케미칼': '011170.KS', '한국전력': '015760.KS',
    '두산에너빌리티': '034020.KS', 'SK이노베이션': '096770.KS', 'GS칼텍스': '078930.KS',
    '삼성물산': '028260.KS', '현대건설': '000720.KS', '에코프로비엠': '247540.KQ',
    '에코프로': '086520.KQ', '카카오뱅크': '323410.KS', '크래프톤': '259960.KS',
    'KX하이텍': '052900.KQ',
}


def is_korean_ticker(ticker: str) -> bool:
    """yfinance ticker가 KOSPI(.KS) 또는 KOSDAQ(.KQ)인지 판별."""
    return bool(ticker) and (ticker.endswith('.KS') or ticker.endswith('.KQ'))


_NAVER_PRICE_URL = "https://m.stock.naver.com/api/stock/{code}/basic"


def get_kr_price_naver(ticker: str) -> float | None:
    """네이버 금융 API로 국내 종목 현재가 조회. 실패 시 None.

    yfinance는 코스닥 소형주에서 장중 가격이 전일 종가에 머물거나 일별 데이터에
    결손이 생긴다(예: KX하이텍 052900 — 거래량 누락 확인됨). 국내 종목 현재가는
    네이버를 1순위로 쓰고 yfinance는 폴백으로만 사용한다.
    """
    code = ticker.split('.')[0]
    if not (code.isdigit() and len(code) == 6):
        return None
    try:
        req = urllib.request.Request(
            _NAVER_PRICE_URL.format(code=code),
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with safe_opener.open(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        price = float(str(data.get("closePrice", "")).replace(",", ""))
        return price if price > 0 else None
    except Exception as e:
        log.warning("get_kr_price_naver(%s) 실패: %s", ticker, e)
        return None


def get_kr_ticker(company: str) -> str | None:
    """DART find_corp으로 종목명 → yfinance ticker (예: 005930.KS) 변환.

    corp_cls 매핑: Y=KOSPI(.KS), K=KOSDAQ(.KQ), N=코넥스(상장폐지 위험·미지원), E=기타.
    상장 종목만 ticker 반환.
    """
    if company in _KR_TICKER_FALLBACK:
        return _KR_TICKER_FALLBACK[company]
    try:
        corp_df = dart.corp_codes
        result = corp_df[corp_df['corp_name'] == company]
        if result is None or result.empty:
            return None
        if 'corp_cls' in result.columns:
            listed = result[result['corp_cls'].isin(['Y', 'K'])]
            row = listed.iloc[0] if not listed.empty else result.iloc[0]
        else:
            row = result.iloc[0]
        stock_code = str(row.get('stock_code', '')).strip()
        if not stock_code or stock_code == 'nan':
            return None
        cls = str(row.get('corp_cls', '')).strip()
        if cls == 'Y':
            suffix = '.KS'
        elif cls == 'K':
            suffix = '.KQ'
        else:
            return None
        return stock_code + suffix
    except Exception as e:
        log.warning("get_kr_ticker(%s) DART 조회 실패: %s", company, e)
        return None


def _prev_close(t) -> float | None:
    """yfinance 공식 전일 종가 (일봉 갭에 강건). 실패 시 None."""
    try:
        pc = t.fast_info.previous_close
        return float(pc) if pc else None
    except Exception:
        return None


def _price_summary(hist, prev_close=None) -> dict:
    """1년치 OHLC → 현재가·전일대비·기간 등락률·52주 고저 (KR/US 공용 계산).

    전일 종가는 yfinance 일봉이 직전 거래일 바를 누락하는 경우가 있어
    (iloc[-2]가 며칠 전을 집어 전일 등락이 다중일로 둔갑) 공식 previous_close를
    우선 사용한다. 없으면 직전 행으로 폴백.
    """
    closes = hist['Close']
    cur  = float(closes.iloc[-1])
    if prev_close and prev_close > 0:
        prev = float(prev_close)
    elif len(closes) > 1:
        prev = float(closes.iloc[-2])
    else:
        prev = cur
    chg  = cur - prev
    return {
        'cur':   cur,
        'chg':   chg,
        'chg_p': chg / prev * 100 if prev else 0.0,
        'w_chg': (cur - closes.iloc[-5])  / closes.iloc[-5]  * 100 if len(hist) >= 5  else 0.0,
        'm_chg': (cur - closes.iloc[-20]) / closes.iloc[-20] * 100 if len(hist) >= 20 else 0.0,
        'ytd':   (cur - closes.iloc[0])   / closes.iloc[0]   * 100,
        'hi52':  hist['High'].max(),
        'lo52':  hist['Low'].min(),
        'arrow': '▲' if chg >= 0 else '▼',
        'sign':  '+' if chg >= 0 else '',
    }


def get_price_info_kr(ticker: str, company: str) -> str:
    """yfinance로 국내 주가 정보 텍스트 생성 (동기 함수 - executor에서 실행)"""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period='1y')
        if hist.empty:
            return ''
        s = _price_summary(hist, _prev_close(t))
        return (
            f"💰 **실시간 주가**\n"
            f"현재가: {s['cur']:,.0f}원  {s['arrow']} {s['sign']}{s['chg']:,.0f} ({s['sign']}{s['chg_p']:.1f}%)\n"
            f"금주: {s['w_chg']:+.1f}%  |  금월: {s['m_chg']:+.1f}%  |  YTD: {s['ytd']:+.1f}%\n"
            f"52주 고: {s['hi52']:,.0f}원  |  저: {s['lo52']:,.0f}원"
        )
    except Exception as e:
        log.warning("get_price_info_kr(%s) 실패: %s", ticker, e)
        return '(주가 조회 실패)'


def get_us_report_text(query: str) -> str:
    """미국 종목 리포트 텍스트 생성 (동기 함수 - executor에서 실행)"""
    ticker = US_STOCK_MAP.get(query.lower(), US_STOCK_MAP.get(query, query.upper()))
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        hist  = stock.history(period='1y')
        if hist.empty:
            return f"❌ '{query}' 종목을 찾을 수 없습니다. (ticker: {ticker})"

        s = _price_summary(hist, _prev_close(stock))

        name      = info.get('longName', ticker)
        sector    = info.get('sector', 'N/A')
        industry  = info.get('industry', 'N/A')
        mkt_cap   = info.get('marketCap', 0) or 0
        per       = info.get('trailingPE') or 0
        fwd_per   = info.get('forwardPE')  or 0
        beta      = info.get('beta')       or 0
        div_yield = (info.get('dividendYield') or 0) * 100

        cap_str = f"${mkt_cap/1e12:.2f}T" if mkt_cap >= 1e12 else f"${mkt_cap/1e9:.1f}B"

        eps_lines = ''
        try:
            cal = stock.earnings_dates
            if cal is not None and not cal.empty:
                recent = cal.dropna(subset=['EPS Estimate', 'Reported EPS']).head(4)
                if not recent.empty:
                    eps_lines = '\n📌 **EPS 서프라이즈 (최근 4분기)**\n'
                    for dt, row in recent.iterrows():
                        est = row['EPS Estimate']
                        act = row['Reported EPS']
                        beat = '✅ Beat' if act >= est else '❌ Miss'
                        eps_lines += f"{str(dt)[:10]} | est ${est:.2f} → actual ${act:.2f}  {beat}\n"
        except Exception:
            pass

        return (
            f"🏢 **{name} ({ticker})**\n"
            f"섹터: {sector} | {industry}\n"
            f"시가총액: {cap_str}\n\n"
            f"💵 **실시간 주가**\n"
            f"현재가: ${s['cur']:.2f}  {s['arrow']} {s['sign']}${s['chg']:.2f} ({s['sign']}{s['chg_p']:.1f}%)\n"
            f"금주: {s['w_chg']:+.1f}%  |  금월: {s['m_chg']:+.1f}%  |  YTD: {s['ytd']:+.1f}%\n"
            f"52W High: ${s['hi52']:.2f}  |  Low: ${s['lo52']:.2f}\n\n"
            f"📊 **밸류에이션**\n"
            f"PER: {per:.1f}x  |  Fwd PER: {fwd_per:.1f}x  |  Beta: {beta:.2f}\n"
            f"배당수익률: {div_yield:.2f}%"
            f"{eps_lines}"
        )
    except Exception as e:
        log.warning("get_us_report_text(%s) 실패: %s", query, e)
        return "⚠️ 미국 종목 조회 중 오류가 발생했습니다."
