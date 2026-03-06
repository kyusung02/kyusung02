"""
주가 데이터 — yfinance 조회, DART 종목 코드 변환, 미국 종목 매핑
"""
import logging
import yfinance as yf
import OpenDartReader
from config import DART_API_KEY

log = logging.getLogger(__name__)
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
}


def get_kr_ticker(company: str) -> str | None:
    """DART find_corp으로 종목명 → yfinance ticker (예: 005930.KS) 변환"""
    if company in _KR_TICKER_FALLBACK:
        return _KR_TICKER_FALLBACK[company]
    try:
        corp_df = dart.corp_codes
        result = corp_df[corp_df['corp_name'] == company]
        if result is None or result.empty:
            return None
        listed = result[result['corp_cls'].isin(['Y', 'K'])]
        row = listed.iloc[0] if not listed.empty else result.iloc[0]
        stock_code = str(row.get('stock_code', '')).strip()
        if not stock_code or stock_code == 'nan':
            return None
        suffix = '.KS' if row.get('corp_cls') == 'Y' else '.KQ'
        return stock_code + suffix
    except Exception as e:
        log.warning(f"get_kr_ticker({company}) DART 조회 실패: {e}")
        return None


def get_price_info_kr(ticker: str, company: str) -> str:
    """yfinance로 국내 주가 정보 텍스트 생성 (동기 함수 - executor에서 실행)"""
    try:
        hist = yf.Ticker(ticker).history(period='1y')
        if hist.empty:
            return ''
        cur   = hist['Close'].iloc[-1]
        prev  = hist['Close'].iloc[-2] if len(hist) > 1 else cur
        chg   = cur - prev
        chg_p = chg / prev * 100

        w_chg = (cur - hist['Close'].iloc[-5])  / hist['Close'].iloc[-5]  * 100 if len(hist) >= 5  else 0.0
        m_chg = (cur - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20] * 100 if len(hist) >= 20 else 0.0
        ytd   = (cur - hist['Close'].iloc[0])   / hist['Close'].iloc[0]   * 100

        hi52 = hist['High'].max()
        lo52 = hist['Low'].min()
        arrow = '▲' if chg >= 0 else '▼'
        sign  = '+' if chg >= 0 else ''
        return (
            f"💰 **실시간 주가**\n"
            f"현재가: {cur:,.0f}원  {arrow} {sign}{chg:,.0f} ({sign}{chg_p:.1f}%)\n"
            f"금주: {w_chg:+.1f}%  |  금월: {m_chg:+.1f}%  |  YTD: {ytd:+.1f}%\n"
            f"52주 고: {hi52:,.0f}원  |  저: {lo52:,.0f}원"
        )
    except Exception as e:
        return f'(주가 조회 실패: {e})'


def get_us_report_text(query: str) -> str:
    """미국 종목 리포트 텍스트 생성 (동기 함수 - executor에서 실행)"""
    ticker = US_STOCK_MAP.get(query.lower(), US_STOCK_MAP.get(query, query.upper()))
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        hist  = stock.history(period='1y')
        if hist.empty:
            return f"❌ '{query}' 종목을 찾을 수 없습니다. (ticker: {ticker})"

        cur   = hist['Close'].iloc[-1]
        prev  = hist['Close'].iloc[-2] if len(hist) > 1 else cur
        chg   = cur - prev
        chg_p = chg / prev * 100

        w_chg = (cur - hist['Close'].iloc[-5])  / hist['Close'].iloc[-5]  * 100 if len(hist) >= 5  else 0.0
        m_chg = (cur - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20] * 100 if len(hist) >= 20 else 0.0
        ytd   = (cur - hist['Close'].iloc[0])   / hist['Close'].iloc[0]   * 100
        hi52  = hist['High'].max()
        lo52  = hist['Low'].min()

        name      = info.get('longName', ticker)
        sector    = info.get('sector', 'N/A')
        industry  = info.get('industry', 'N/A')
        mkt_cap   = info.get('marketCap', 0) or 0
        per       = info.get('trailingPE') or 0
        fwd_per   = info.get('forwardPE')  or 0
        beta      = info.get('beta')       or 0
        div_yield = (info.get('dividendYield') or 0) * 100

        cap_str = f"${mkt_cap/1e12:.2f}T" if mkt_cap >= 1e12 else f"${mkt_cap/1e9:.1f}B"
        arrow   = '▲' if chg >= 0 else '▼'
        sign    = '+' if chg >= 0 else ''

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
            f"현재가: ${cur:.2f}  {arrow} {sign}${chg:.2f} ({sign}{chg_p:.1f}%)\n"
            f"금주: {w_chg:+.1f}%  |  금월: {m_chg:+.1f}%  |  YTD: {ytd:+.1f}%\n"
            f"52W High: ${hi52:.2f}  |  Low: ${lo52:.2f}\n\n"
            f"📊 **밸류에이션**\n"
            f"PER: {per:.1f}x  |  Fwd PER: {fwd_per:.1f}x  |  Beta: {beta:.2f}\n"
            f"배당수익률: {div_yield:.2f}%"
            f"{eps_lines}"
        )
    except Exception as e:
        return f"⚠️ 오류가 발생했습니다: {e}"
