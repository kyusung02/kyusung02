"""
DART 핸들러 — 재무 분석, 공시 조회, 관심종목 공시 자동 감지
"""
import re
import logging
import asyncio
import urllib.request
import urllib.parse
from datetime import date, timedelta
import pandas as pd
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID, DART_ALERT_KEYWORDS
from services.gemini import client, FINANCE_PROMPT
from services.stock import dart, get_kr_ticker
from storage import load_watchlist, load_seen_filings, save_seen_filings
from utils import safe_opener

log = logging.getLogger(__name__)


def _stock_code_from_ticker(ticker: str) -> str:
    """yfinance ticker에서 종목코드만 추출 (예: 005930.KS → 005930)"""
    return ticker.split('.')[0]


def _get_finance_summary_sync(company: str) -> str:
    """DART 조회 + Gemini 분석 (동기 함수 - executor에서 실행)"""
    try:
        year = 2024
        df = dart.finstate_all(company, year)
        if df is None or df.empty:
            year = 2023
            df = dart.finstate_all(company, year)

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

        today    = date.today().strftime("%Y년 %m월 %d일")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[FINANCE_PROMPT + f"\n\n작성일: {today}\n종목: {company}\n데이터:\n{data_str}"]
        )
        return response.text + f"\n\n📌 출처: DART 전자공시시스템 ({year}년 재무제표)"
    except Exception as e:
        return f"⚠️ 분석 중 오류가 발생했습니다: {e}"


async def get_finance_summary(company: str) -> str:
    """DART 재무 분석 async 래퍼"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _get_finance_summary_sync, company)


def _get_corp_overview_sync(company: str, ticker: str) -> str:
    """DART + yfinance로 기업 개요 텍스트 생성 (동기 함수 - executor에서 실행)"""
    import yfinance as yf
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
            lines.append(f"거래소: {cls_name}  |  종목코드: {_stock_code_from_ticker(ticker)}")
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
            except Exception:
                pass
    except Exception:
        pass
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
    except Exception:
        pass
    return '\n'.join(lines)


def _get_dart_recent_filings_sync(company: str) -> str:
    """DART 최근 3개월 중요 공시 최대 5건 (동기 함수 - executor에서 실행)"""
    try:
        start_dt = (date.today() - timedelta(days=90)).strftime('%Y-%m-%d')
        reports  = dart.list(company, start=start_dt)
        if reports is None or reports.empty:
            return "📋 **최근 주요 공시**\n최근 3개월 내 공시 없음"
        IMPORTANT_KW = [
            '잠정실적', '시설투자', '내부자', '자기주식', '최대주주',
            '유상증자', '합병', '분할', '전환사채', '임원변동', '특수관계인',
        ]
        def score(nm: str) -> int:
            return sum(1 for kw in IMPORTANT_KW if kw in nm)
        sorted_rows = sorted(reports.iterrows(), key=lambda x: score(str(x[1]['report_nm'])), reverse=True)
        lines = ["📋 **최근 주요 공시** (3개월)"]
        for _, row in sorted_rows[:5]:
            nm    = str(row['report_nm'])
            dt    = str(row['rcept_dt'])
            rcpNo = str(row['rcept_no'])
            link  = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpNo}"
            flag  = "🚨" if score(nm) > 0 else "📢"
            lines.append(f"{flag} {dt} | [{nm}]({link})")
        return '\n'.join(lines)
    except Exception as e:
        return f"📋 **최근 주요 공시**\n(조회 실패: {e})"


def _get_quarterly_financials_text_sync(ticker: str) -> str:
    """yfinance 분기 실적 텍스트 테이블 (최근 6분기, QoQ%, TTM) (동기 함수)"""
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).quarterly_income_stmt
        if df is None or df.empty:
            return "📊 **분기 실적**\n(데이터 없음)"
        df = df.sort_index(axis=1).iloc[:, -6:]

        def _find_row(candidates):
            for name in candidates:
                if name in df.index:
                    return df.loc[name]
            return None

        rev = _find_row(['Total Revenue', 'Revenue'])
        oi  = _find_row(['Operating Income', 'EBIT', 'Operating Income Loss'])
        if rev is None:
            return "📊 **분기 실적**\n(매출 데이터 없음)"

        is_krw = ticker.endswith('.KS') or ticker.endswith('.KQ')
        unit   = 1e12 if is_krw else 1e9
        ulabel = '조원' if is_krw else 'B$'

        def _ql(dt):
            return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"

        labels   = [_ql(c) for c in df.columns]
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
        return f"📊 **분기 실적**\n(조회 실패: {e})"


def _get_naver_research_sync(company: str) -> str:
    """네이버 금융 증권사 리포트 크롤링 최근 5건 (동기 함수 - executor에서 실행)"""
    try:
        encoded = urllib.parse.quote(company)
        url = f"https://finance.naver.com/research/company_list.nhn?keyword={encoded}&x=0&y=0"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with safe_opener.open(req, timeout=10) as resp:
            html = resp.read().decode('euc-kr', errors='ignore')
        entries = re.findall(
            r'href="(/research/company_read\.nhn\?nid=\d+)"[^>]*>\s*([^<]{5,100})\s*</a>',
            html,
        )
        if not entries:
            return "📄 **증권사 리포트**\n(최근 리포트 없음)"
        lines = ["📄 **증권사 리포트** (최근)"]
        seen, count = set(), 0
        for path, title in entries:
            title = title.strip()
            if title in seen or len(title) < 5:
                continue
            seen.add(title)
            lines.append(f"• [{title}](https://finance.naver.com{path})")
            count += 1
            if count >= 5:
                break
        return '\n'.join(lines) if count > 0 else "📄 **증권사 리포트**\n(최근 리포트 없음)"
    except Exception as e:
        return f"📄 **증권사 리포트**\n(조회 실패: {e})"


async def check_dart_watchlist():
    """관심종목 DART 공시를 주기적으로 확인합니다."""
    stocks = load_watchlist()
    if not stocks:
        return

    seen    = load_seen_filings()
    today   = date.today().strftime('%Y-%m-%d')
    loop    = asyncio.get_running_loop()
    new_seen = set(seen)

    for comp in stocks:
        try:
            reports = await loop.run_in_executor(_executor, dart.list, comp, today)
            if reports is None or reports.empty:
                continue
            for _, row in reports.iterrows():
                rcpNo = str(row['rcept_no'])
                if rcpNo in seen:
                    continue
                report_nm = str(row['report_nm'])
                is_alert  = any(kw in report_nm for kw in DART_ALERT_KEYWORDS)
                flag      = "🚨" if is_alert else "📢"
                link      = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpNo}"
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"{flag} **[관심종목 공시]** {comp}\n"
                    f"▪️ {row['rcept_dt']} | [{report_nm}]({link})",
                    link_preview=False,
                )
                new_seen.add(rcpNo)
        except Exception as e:
            log.warning("DART watchlist check failed for %s: %s", comp, e)

    if new_seen != seen:
        save_seen_filings(new_seen)
