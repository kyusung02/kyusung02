"""
DART/yfinance 데이터 페칭 — 동기 함수 (executor에서 실행). 모두 외부 부수효과 없음.
"""
import re
import logging
import urllib.request
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
from services.gemini import FINANCE_PROMPT, generate_with_retry
from services.stock import dart, is_korean_ticker
from services.chart import find_row, quarter_label, financial_unit
from utils import safe_opener

log = logging.getLogger(__name__)

DART_FILING_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

IMPORTANT_FILING_KEYWORDS = (
    '잠정실적', '시설투자', '내부자', '자기주식', '최대주주',
    '유상증자', '합병', '분할', '전환사채', '임원변동', '특수관계인',
)

_MD_ESCAPE_RE = re.compile(r'([_*\[\]`])')


def stock_code_from_ticker(ticker: str) -> str:
    """yfinance ticker에서 종목코드만 추출 (예: 005930.KS → 005930)."""
    return ticker.split('.')[0]


def _escape_md(text: str) -> str:
    r"""Telegram 마크다운 메타문자(_ * [ ] `) 이스케이프."""
    return _MD_ESCAPE_RE.sub(r'\\\1', text or '')


def get_finance_summary_sync(company: str) -> str:
    """DART 재무 조회 + Gemini 분석 (동기 함수)."""
    try:
        current_year = date.today().year - 1
        df = None
        used_year = None
        for year in (current_year, current_year - 1):
            df = dart.finstate_all(company, year)
            if df is not None and not df.empty:
                used_year = year
                break

        if df is None or df.empty:
            return f"❌ '{company}'의 재무 데이터를 찾을 수 없습니다. (종목명/상장여부 확인)"

        essential = df[df['account_nm'].str.contains('매출액|영업이익|당기순이익', na=False)]
        amount_col = next(
            (c for c in ['thstrm_amount', 'thstrm_add_amount'] if c in essential.columns),
            None
        )
        if amount_col is None:
            return f"⚠️ 분석 중 오류가 발생했습니다: 당기금액 컬럼을 찾을 수 없습니다. (컬럼: {list(essential.columns)})"
        cols = ['account_nm', amount_col]
        if 'frmtrm_amount' in essential.columns:
            cols.append('frmtrm_amount')
        data_str = essential[cols].to_string()

        today = date.today().strftime("%Y년 %m월 %d일")
        response = generate_with_retry(
            [FINANCE_PROMPT + f"\n\n작성일: {today}\n종목: {company}\n데이터:\n{data_str}"]
        )
        return response.text + f"\n\n📌 출처: DART 전자공시시스템 ({used_year}년 재무제표)"
    except Exception as e:
        log.warning("get_finance_summary_sync(%s) 실패: %s", company, e)
        return "⚠️ 재무 분석 중 오류가 발생했습니다."


def get_corp_overview_sync(company: str, ticker: str) -> str:
    """DART + yfinance로 기업 개요 텍스트 생성 (동기 함수)."""
    lines = ["🏢 **기업 개요**"]
    try:
        corp_df = dart.corp_codes
        result  = corp_df[corp_df['corp_name'] == company]
        if result is not None and not result.empty:
            listed   = result[result['corp_cls'].isin(['Y', 'K'])]
            row      = listed.iloc[0] if not listed.empty else result.iloc[0]
            cls_map  = {'Y': 'KOSPI', 'K': 'KOSDAQ', 'N': '코넥스', 'E': '기타'}
            cls_name = cls_map.get(str(row.get('corp_cls', '')), '기타')
            corp_code = str(row.get('corp_code', ''))
            lines.append(f"거래소: {cls_name}  |  종목코드: {stock_code_from_ticker(ticker)}")
            try:
                info = dart.company(corp_code)
                if info is not None:
                    ceo = info.get('ceo_nm', '')
                    acc = info.get('acc_mt', '')
                    hm  = info.get('hm_url', '')
                    if ceo:
                        lines.append(f"대표이사: {ceo}")
                    if acc:
                        lines.append(f"결산월: {acc}월")
                    if hm:
                        lines.append(f"홈페이지: {hm}")
            except Exception as e:
                log.debug("DART company(%s) 조회 실패: %s", corp_code, e)
    except Exception as e:
        log.debug("DART corp_codes 조회 실패 (%s): %s", company, e)
    try:
        info     = yf.Ticker(ticker).info
        sector   = info.get('sector', '')
        industry = info.get('industry', '')
        emp      = info.get('fullTimeEmployees', 0)
        if sector:
            lines.append(f"섹터: {sector}")
        if industry:
            lines.append(f"업종: {industry}")
        if emp:
            lines.append(f"임직원: {emp:,}명")
    except Exception as e:
        log.debug("yfinance info(%s) 조회 실패: %s", ticker, e)
    return '\n'.join(lines)


def get_dart_recent_filings_sync(company: str) -> str:
    """DART 최근 3개월 중요 공시 최대 5건 (동기 함수)."""
    try:
        start_dt = (date.today() - timedelta(days=90)).strftime('%Y-%m-%d')
        reports  = dart.list(company, start=start_dt)
        if reports is None or reports.empty:
            return "📋 **최근 주요 공시**\n최근 3개월 내 공시 없음"

        def score(nm: str) -> int:
            return sum(1 for kw in IMPORTANT_FILING_KEYWORDS if kw in nm)

        sorted_rows = sorted(reports.iterrows(), key=lambda x: score(str(x[1]['report_nm'])), reverse=True)
        lines = ["📋 **최근 주요 공시** (3개월)"]
        for _, row in sorted_rows[:5]:
            nm    = str(row['report_nm'])
            dt    = str(row['rcept_dt'])
            rcpNo = str(row['rcept_no'])
            link  = DART_FILING_URL.format(rcpNo)
            flag  = "🚨" if score(nm) > 0 else "📢"
            lines.append(f"{flag} {dt} | [{nm}]({link})")
        return '\n'.join(lines)
    except Exception as e:
        log.warning("get_dart_recent_filings_sync(%s) 실패: %s", company, e)
        return "📋 **최근 주요 공시**\n(조회 실패)"


def get_quarterly_financials_text_sync(ticker: str) -> str:
    """yfinance 분기 실적 텍스트 테이블 (최근 6분기, QoQ%, TTM) (동기 함수)."""
    try:
        df = yf.Ticker(ticker).quarterly_income_stmt
        if df is None or df.empty:
            return "📊 **분기 실적**\n(데이터 없음)"
        df = df.sort_index(axis=1).iloc[:, -6:]

        rev = find_row(df, ['Total Revenue', 'Revenue'])
        oi  = find_row(df, ['Operating Income', 'EBIT', 'Operating Income Loss'])
        if rev is None:
            return "📊 **분기 실적**\n(매출 데이터 없음)"

        unit, ulabel = financial_unit(ticker)
        labels = [quarter_label(c) for c in df.columns]
        rev_vals = pd.to_numeric(rev, errors='coerce') / unit
        oi_vals  = pd.to_numeric(oi,  errors='coerce') / unit if oi is not None else None

        lines = [f"📊 **분기 실적** ({ulabel})\n```"]
        lines.append(f"{'분기':<7} {'매출':>6} {'영업이익':>7} {'OPM':>5} {'QoQ%':>7}")
        lines.append("-" * 38)
        for i, lbl in enumerate(labels):
            rv  = rev_vals.iloc[i]
            ov  = oi_vals.iloc[i] if oi_vals is not None else float('nan')
            opm = (ov / rv * 100) if not (pd.isna(rv) or pd.isna(ov)) and rv != 0 else float('nan')
            qoq = float('nan')
            if i > 0:
                prev = rev_vals.iloc[i - 1]
                if not pd.isna(prev) and prev != 0:
                    qoq = (rv - prev) / abs(prev) * 100
            rv_s  = f"{rv:.1f}"   if not pd.isna(rv)  else "-"
            ov_s  = f"{ov:.1f}"   if not pd.isna(ov)  else "-"
            opm_s = f"{opm:.1f}%" if not pd.isna(opm) else "-"
            qoq_s = f"{qoq:+.1f}%" if not pd.isna(qoq) else "-"
            lines.append(f"{lbl:<7} {rv_s:>6} {ov_s:>7} {opm_s:>5} {qoq_s:>7}")

        rev_n = pd.to_numeric(rev, errors='coerce')
        oi_n  = pd.to_numeric(oi,  errors='coerce') if oi is not None else None
        if len(rev_n.dropna()) >= 4:
            ttm_rev = rev_n.iloc[-4:].sum() / unit
            ttm_oi  = oi_n.iloc[-4:].sum() / unit if oi_n is not None else None
            ttm_opm = (ttm_oi / ttm_rev * 100) if ttm_oi is not None and ttm_rev != 0 else None
            lines.append("-" * 38)
            oi_s  = f"{ttm_oi:.1f}"   if ttm_oi  is not None else "-"
            opm_s = f"{ttm_opm:.1f}%" if ttm_opm is not None else "-"
            lines.append(f"{'TTM':<7} {ttm_rev:>6.1f} {oi_s:>7} {opm_s:>5}")
        lines.append("```")
        return '\n'.join(lines)
    except Exception as e:
        log.warning("get_quarterly_financials_text_sync(%s) 실패: %s", ticker, e)
        return "📊 **분기 실적**\n(조회 실패)"


def _fetch_research_list_html() -> str:
    """네이버 금융 종목분석 리스트 페이지 HTML 본문."""
    url = "https://finance.naver.com/research/company_list.naver"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with safe_opener.open(req, timeout=10) as resp:
        return resp.read().decode('euc-kr', errors='ignore')


def _parse_research_rows(html: str) -> list[dict]:
    """리스트 페이지 HTML의 <tr> 행을 {stock,title,broker,date,link} 로 파싱."""
    rows = re.findall(r'<tr>(.*?)</tr>', html, flags=re.DOTALL)
    parsed = []
    for row in rows:
        sm = re.search(r'class="stock_item"[^>]*>([^<]+)</a>', row)
        if not sm:
            continue
        tm = re.search(r'<a href="(company_read\.naver\?nid=\d+[^"]*)"[^>]*>([^<]+)</a>', row)
        if not tm:
            continue
        # 제목 td 직후의 첫 plain <td>...</td> = 증권사명
        bm = re.search(r'</td>\s*<td>([^<]+)</td>', row[tm.end():])
        # PDF 직접 링크가 있으면 우선(없으면 read 페이지로 fallback)
        pm = re.search(r'class="file"[^>]*>\s*<a href="(https://stock\.pstatic\.net/[^"]+\.pdf)"', row)
        dm = re.search(r'class="date"[^>]*>([\d.]+)</td>', row)
        read_url = "https://finance.naver.com/research/" + tm.group(1).replace('&amp;', '&')
        parsed.append({
            'stock':  sm.group(1).strip(),
            'title':  tm.group(2).strip(),
            'broker': bm.group(1).strip() if bm else '',
            'date':   dm.group(1).strip() if dm else '',
            'link':   pm.group(1) if pm else read_url,
        })
    return parsed


def get_naver_research_sync(company: str) -> str:
    """특정 종목의 최근 증권사 리포트 최대 5건 (텔레그램 마크다운 텍스트).

    네이버가 keyword 검색을 무력화해(2026년 확인) 전체 종목 분석 리스트를 받아
    클라이언트단에서 종목명으로 필터링한다.
    """
    try:
        html = _fetch_research_list_html()
    except Exception as e:
        log.warning("get_naver_research_sync(%s) 실패: %s", company, e)
        return "📄 **증권사 리포트**\n(조회 실패)"

    target = company.strip()
    lines, seen, count = ["📄 **증권사 리포트** (최근)"], set(), 0
    for r in _parse_research_rows(html):
        if r['stock'] != target and target not in r['stock']:
            continue
        if r['title'] in seen or len(r['title']) < 3:
            continue
        seen.add(r['title'])
        entry = f"• [{_escape_md(r['title'])}]({r['link']})"
        if r['broker']:
            entry += f" — {_escape_md(r['broker'])}"
        if r['date']:
            entry += f" ({r['date']})"
        lines.append(entry)
        count += 1
        if count >= 5:
            break

    if count == 0:
        return "📄 **증권사 리포트**\n(최근 리포트 없음)"
    return '\n'.join(lines)


def get_today_research_sync(today: str | None = None) -> list[dict]:
    """오늘(또는 지정 'YY.MM.DD') 발행된 증권사 종목분석 리포트 목록.

    네이버 종목분석 리스트는 최신 30건만 노출되므로 하루 분량은 거의 다 잡힌다.
    토·일·공휴일에는 거의 비어있다(빈 리스트 반환).
    """
    if today is None:
        today = date.today().strftime('%y.%m.%d')
    try:
        html = _fetch_research_list_html()
    except Exception as e:
        log.warning("get_today_research_sync 실패: %s", e)
        return []
    return [r for r in _parse_research_rows(html) if r['date'] == today]
