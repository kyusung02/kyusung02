"""
DART 핸들러 — 관심종목 공시 자동 감지 (데이터 페칭은 services.dart_service에 위임).
"""
import logging
import asyncio
from datetime import date
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID, DART_ALERT_KEYWORDS
from services.dart_service import (
    DART_FILING_URL,
    stock_code_from_ticker,
    get_finance_summary_sync,
    get_corp_overview_sync,
    get_dart_recent_filings_sync,
    get_quarterly_financials_text_sync,
    get_naver_research_sync,
)
from services.stock import dart
from storage import load_watchlist, load_seen_filings, save_seen_filings

log = logging.getLogger(__name__)


# 하위 호환 — main.py / report.py에서 기존 이름으로 import 가능하도록 노출
_stock_code_from_ticker          = stock_code_from_ticker
_get_finance_summary_sync        = get_finance_summary_sync
_get_corp_overview_sync          = get_corp_overview_sync
_get_dart_recent_filings_sync    = get_dart_recent_filings_sync
_get_quarterly_financials_text_sync = get_quarterly_financials_text_sync
_get_naver_research_sync         = get_naver_research_sync


async def get_finance_summary(company: str) -> str:
    """DART 재무 분석 async 래퍼."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, get_finance_summary_sync, company)


async def check_dart_watchlist():
    """관심종목 DART 공시를 주기적으로 확인합니다."""
    stocks = load_watchlist()
    if not stocks:
        return

    seen     = load_seen_filings()
    today    = date.today().strftime('%Y-%m-%d')
    loop     = asyncio.get_running_loop()
    new_seen = list(seen)

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
                link      = DART_FILING_URL.format(rcpNo)
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"{flag} **[관심종목 공시]** {comp}\n"
                    f"▪️ {row['rcept_dt']} | [{report_nm}]({link})",
                    link_preview=False,
                )
                seen.add(rcpNo)
                new_seen.append(rcpNo)
        except Exception as e:
            log.warning("DART watchlist check failed for %s: %s", comp, e)

    if len(new_seen) > len(load_seen_filings()):
        save_seen_filings(new_seen)
