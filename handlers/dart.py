"""
DART 핸들러 — 관심종목 공시 자동 감지 (데이터 페칭은 services.dart_service에 위임).

어닝 키워드가 잡힌 공시는 발견 직후 get_finance_summary_sync 로 자동
분기 실적 분석을 함께 발송한다(국내 종목 컨센서스 컨텍스트의 출발점).
"""
import logging
import asyncio
from clients import bot_client, _executor
from utils import kst_today
from config import BROADCAST_ID, DART_ALERT_KEYWORDS
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

# 어닝(분기 실적) 발표를 식별하는 키워드. 매칭 시 자동 재무 분석 첨부.
_EARNINGS_KEYWORDS = (
    '잠정실적', '연결실적', '영업실적', '매출액',
    '분기보고서', '반기보고서', '사업보고서',
)


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
    today    = kst_today().strftime('%Y-%m-%d')
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
                is_alert    = any(kw in report_nm for kw in DART_ALERT_KEYWORDS)
                is_earnings = any(kw in report_nm for kw in _EARNINGS_KEYWORDS)
                flag = "📊" if is_earnings else ("🚨" if is_alert else "📢")
                link = DART_FILING_URL.format(rcpNo)

                await bot_client.send_message(
                    BROADCAST_ID,
                    f"{flag} **[관심종목 공시]** {comp}\n"
                    f"▪️ {row['rcept_dt']} | [{report_nm}]({link})",
                    link_preview=False,
                )

                # 어닝 발표면 자동 재무 분석 첨부 (Gemini 호출).
                # 비싼 호출이라 watchlist 알림 본체와 분리해 실패 시 알림 자체엔 영향 없음.
                if is_earnings:
                    try:
                        fin_text = await loop.run_in_executor(
                            _executor, get_finance_summary_sync, comp,
                        )
                        await bot_client.send_message(
                            BROADCAST_ID,
                            f"📈 **[자동 재무 분석]** {comp}\n\n{fin_text}",
                        )
                    except Exception as e:
                        log.warning("자동 재무 분석 실패 %s: %s", comp, e)

                seen.add(rcpNo)
                new_seen.append(rcpNo)
        except Exception as e:
            log.warning("DART watchlist check failed for %s: %s", comp, e)

    if len(new_seen) > len(load_seen_filings()):
        save_seen_filings(new_seen)
