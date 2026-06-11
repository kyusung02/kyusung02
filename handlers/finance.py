"""
종목 조회 핸들러 — /재무(DART 재무+차트), /us(미국 종목), /공시, /watchlist 목록.

main.py 에서 분리(2026-06): main.py 는 라우팅·스케줄러만 담당한다.
"""
import os
import logging
import asyncio
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID, CHARTS_DIR
from services.stock import (
    get_kr_ticker, get_us_report_text, get_price_info_kr, US_STOCK_MAP, dart,
)
from services.chart import _draw_chart_kr, _draw_chart_us, _draw_chart_financials
from services.dart_service import DART_FILING_URL, get_finance_summary_sync
from storage import load_watchlist
from utils import reply_chunked, kst_today, safe_filename, cleanup_files

log = logging.getLogger(__name__)


async def handle_finance(event, comp: str):
    """`/재무` — DART 재무 분석 + 주가 + 차트."""
    await event.reply(f"📊 **{comp}** 분석 중... (주가 조회 + DART 재무 + 차트 생성)")

    loop       = asyncio.get_running_loop()
    ticker     = get_kr_ticker(comp)
    fin_task   = loop.run_in_executor(_executor, get_finance_summary_sync, comp)
    price_task = loop.run_in_executor(_executor, get_price_info_kr, ticker, comp) if ticker else None

    if price_task:
        fin_text, price_text = await asyncio.gather(fin_task, price_task)
    else:
        fin_text   = await fin_task
        price_text = ''

    charts = []
    if ticker:
        fname    = safe_filename(comp)
        path_d   = os.path.join(CHARTS_DIR, f"{fname}_daily.png")
        path_w   = os.path.join(CHARTS_DIR, f"{fname}_weekly.png")
        path_fin = os.path.join(CHARTS_DIR, f"{fname}_financials.png")
        await loop.run_in_executor(_executor, _draw_chart_kr, ticker, comp, path_d, path_w)
        await loop.run_in_executor(_executor, _draw_chart_financials, ticker, comp, path_fin)
        charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]

    header = f"📈 **[{comp} 투자 분석 리포트]**\n\n"
    if price_text:
        header += price_text + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    await reply_chunked(event, header + fin_text)

    if charts:
        await bot_client.send_file(MY_TELEGRAM_ID, charts)
        cleanup_files(charts)


async def handle_us(event, query: str):
    """`/us` — 미국 종목 리포트 + 차트."""
    await event.reply(f"🇺🇸 **{query}** 미국 종목 조회 중...")

    loop        = asyncio.get_running_loop()
    ticker      = US_STOCK_MAP.get(query.lower(), US_STOCK_MAP.get(query, query.upper()))
    tk          = safe_filename(ticker)
    path_d      = os.path.join(CHARTS_DIR, f"US_{tk}_daily.png")
    path_w      = os.path.join(CHARTS_DIR, f"US_{tk}_weekly.png")
    path_fin    = os.path.join(CHARTS_DIR, f"US_{tk}_financials.png")
    report_task = loop.run_in_executor(_executor, get_us_report_text, query)
    chart_task  = loop.run_in_executor(_executor, _draw_chart_us, query, path_d, path_w)

    report_text = await report_task
    await chart_task
    await loop.run_in_executor(_executor, _draw_chart_financials, ticker, query, path_fin)

    await reply_chunked(event, f"📊 **[미국 종목 리포트]**\n\n{report_text}")

    charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]
    if charts:
        await bot_client.send_file(MY_TELEGRAM_ID, charts)
        cleanup_files(charts)


async def handle_dart_filings(event, comp: str):
    """`/공시` — 최근 3건 공시. 조회 범위는 당해 연초(1월 1일)부터 (YTD)."""
    loop     = asyncio.get_running_loop()
    start_dt = kst_today().replace(month=1, day=1).strftime('%Y-%m-%d')
    reports  = await loop.run_in_executor(
        _executor, lambda: dart.list(comp, start=start_dt)
    )
    if reports is None or reports.empty:
        await event.reply(f"❌ '{comp}'의 최근 공시가 없습니다.")
        return
    msg = f"📑 **[{comp} 최근 주요 공시]**\n\n"
    for _, row in reports.head(3).iterrows():
        link = DART_FILING_URL.format(row['rcept_no'])
        msg += f"▪️ {row['rcept_dt']} | [{row['report_nm']}]({link})\n"
    await event.reply(msg, link_preview=False)


async def handle_watchlist(event):
    """`/watchlist` — 관심종목 전체 목록."""
    stocks = load_watchlist()
    if not stocks:
        await event.reply("📋 관심종목이 비어 있습니다.\n\n/watch 삼성전자  →  추가")
        return
    msg = f"📋 **관심종목 ({len(stocks)}종목)**\n\n"
    for i, s in enumerate(stocks, 1):
        msg += f"{i}. {s}\n"
    msg += "\n/watch 종목명  →  추가\n/unwatch 종목명  →  삭제"
    await event.reply(msg)
