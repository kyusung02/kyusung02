"""
종합 리포트 핸들러 — /report 국내 종목 종합 리포트 (차트 6종 + 텍스트)
"""
import os
import logging
import asyncio
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID, CHARTS_DIR
from services.stock import get_kr_ticker, get_price_info_kr
from services.chart import _draw_chart_kr, _draw_report_financials, _draw_chart_investor_flow
from handlers.dart import (
    _get_corp_overview_sync, _get_dart_recent_filings_sync,
    _get_quarterly_financials_text_sync, _get_naver_research_sync,
    _stock_code_from_ticker,
)

log = logging.getLogger(__name__)


async def handle_report(event, company: str):
    """국내 종목 종합 리포트 생성 + 차트 6종 앨범 전송"""
    await event.reply(
        f"📊 **{company}** 종합 리포트 생성 중...\n"
        "⏳ 기업개요 · 주가 · 공시 · 분기실적 · 증권사리포트 · 차트 6종"
    )
    loop   = asyncio.get_running_loop()
    ticker = get_kr_ticker(company)
    if not ticker:
        return await event.reply(f"❌ '{company}' 종목을 찾을 수 없습니다.\n(DART 등록 종목명으로 검색해주세요)")

    stock_code = _stock_code_from_ticker(ticker)

    overview_text, price_text, filings_text, fin_text, research_text = await asyncio.gather(
        loop.run_in_executor(_executor, _get_corp_overview_sync, company, ticker),
        loop.run_in_executor(_executor, get_price_info_kr, ticker, company),
        loop.run_in_executor(_executor, _get_dart_recent_filings_sync, company),
        loop.run_in_executor(_executor, _get_quarterly_financials_text_sync, ticker),
        loop.run_in_executor(_executor, _get_naver_research_sync, company),
    )

    path_pnl      = os.path.join(CHARTS_DIR, f"{company}_pnl.png")
    path_ttm_rev  = os.path.join(CHARTS_DIR, f"{company}_ttm_rev.png")
    path_ttm_op   = os.path.join(CHARTS_DIR, f"{company}_ttm_op.png")
    path_daily    = os.path.join(CHARTS_DIR, f"{company}_rpt_daily.png")
    path_weekly   = os.path.join(CHARTS_DIR, f"{company}_rpt_weekly.png")
    path_investor = os.path.join(CHARTS_DIR, f"{company}_investor.png")

    await asyncio.gather(
        loop.run_in_executor(_executor, _draw_report_financials, ticker, company, path_pnl, path_ttm_rev, path_ttm_op),
        loop.run_in_executor(_executor, _draw_chart_kr, ticker, company, path_daily, path_weekly),
        loop.run_in_executor(_executor, _draw_chart_investor_flow, stock_code, company, path_investor),
    )

    sep  = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    full = (
        f"📈 **[{company} 종합 리포트]**\n\n"
        f"{overview_text}{sep}"
        f"{price_text}{sep}"
        f"{filings_text}{sep}"
        f"{fin_text}{sep}"
        f"{research_text}"
    )
    for i in range(0, len(full), 4096):
        await event.reply(full[i:i + 4096])

    chart_order = [path_pnl, path_ttm_rev, path_ttm_op, path_daily, path_weekly, path_investor]
    charts = [p for p in chart_order if os.path.exists(p)]
    if charts:
        await bot_client.send_file(MY_TELEGRAM_ID, charts)
        for p in charts:
            try:
                os.remove(p)
            except Exception:
                pass
