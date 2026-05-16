"""네모 봇 (Nemo Bot) — 텔레그램 올인원 자동화 봇"""
import os
import re
import sys
import asyncio
import logging
from datetime import date
from telethon import events
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Sentry 에러 트래킹. DSN 미설정 또는 패키지 미설치 시 조용히 비활성.
# VM 측: pip install -r requirements.txt 후 .env 에 SENTRY_DSN=https://... 추가
try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None

_SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if _SENTRY_DSN:
    if sentry_sdk:
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            environment=os.environ.get("SENTRY_ENV", "production"),
            traces_sample_rate=0.0,
            send_default_pii=False,
        )
    else:
        print("WARNING: SENTRY_DSN 설정됐으나 sentry-sdk 미설치 — pip install -r requirements.txt 실행 필요",
              file=sys.stderr)

from config import (
    TELEGRAM_BOT_TOKEN, MY_TELEGRAM_ID,
    DOWNLOAD_DIR, CHARTS_DIR, DART_ALERT_KEYWORDS,
    CHANNEL_SUMMARY_ENABLED,
)
from clients import user_client, bot_client, _executor
from storage import (
    load_watchlist, add_to_watchlist, remove_from_watchlist,
    load_channels, save_channels,
)
from services.gemini import (
    LINK_PROMPT, generate_with_retry, _wrap_external, analyze_pdf,
)
from services.stock import (
    get_kr_ticker, get_us_report_text, get_price_info_kr, US_STOCK_MAP, dart,
)
from services.chart import _draw_chart_kr, _draw_chart_us, _draw_chart_financials
from services.dart_service import DART_FILING_URL, get_finance_summary_sync, get_dart_recent_filings_sync
from handlers.channel import on_channel_msg, update_channel_handler
from handlers.dart import check_dart_watchlist
from handlers.market import send_us_morning
from handlers.sector import send_kr_sector_briefing
from handlers.report import handle_report
from handlers.life import send_weekly_info
from utils import extract_youtube_id, fetch_webpage_text, reply_chunked
from youtube_transcript_api import YouTubeTranscriptApi

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CHARTS_DIR, exist_ok=True)


# ==========================================
# 봇 메시지 핸들러 (명령어 라우팅)
# ==========================================
@bot_client.on(events.NewMessage())
async def on_bot_msg(event):
    """봇에게 전달되는 메시지를 명령어별 분기로 처리. 본인 계정만 응답."""
    if event.chat_id != MY_TELEGRAM_ID:
        return
    text = event.text or ''
    loop = asyncio.get_running_loop()

    # ── 시황 ────────────────────────────────────────────────────────────────
    if text == '/시황':
        await event.reply("🌅 미국 시황 데이터 수집 중...")
        await send_us_morning()
        return

    # ── 섹터 브리핑 ──────────────────────────────────────────────────────────
    if text == '/섹터':
        await event.respond("📊 섹터 데이터 수집 중... (약 20~30초 소요)")
        await send_kr_sector_briefing()
        return

    # ── 종합 리포트 ─────────────────────────────────────────────────────────
    if text.startswith('/report') or text.startswith('/리포트'):
        comp = text.replace('/report', '').replace('/리포트', '').strip()
        if not comp:
            await event.reply("사용법: /report 삼성전자")
            return
        await handle_report(event, comp)
        return

    # ── 재무 분석 ───────────────────────────────────────────────────────────
    if text.startswith('/재무'):
        comp = text.replace('/재무', '').strip()
        if not comp:
            await event.reply("사용법: /재무 삼성전자")
            return
        await _cmd_finance(event, comp, loop)
        return

    # ── 생활 브리핑 ─────────────────────────────────────────────────────────
    if text == '/장보기':
        await send_weekly_info('shop')
        return
    if text == '/나들이':
        await send_weekly_info('out')
        return

    # ── 미국 주식 ───────────────────────────────────────────────────────────
    if text.lower().startswith('/us '):
        query = text[4:].strip()
        if not query:
            await event.reply("사용법: /us NVDA")
            return
        await _cmd_us(event, query, loop)
        return

    # ── DART 공시 ───────────────────────────────────────────────────────────
    if text.startswith('/공시'):
        comp = text.replace('/공시', '').strip()
        if not comp:
            await event.reply("사용법: /공시 삼성전자")
            return
        await _cmd_dart_filings(event, comp, loop)
        return

    # ── 관심종목 관리 ────────────────────────────────────────────────────────
    # 주의: /unwatch는 반드시 /watchlist 보다 뒤에 두지 말고, 공백 필수로 처리해
    # /unwatchlist 등의 명령이 잘못 매칭되지 않도록 한다.
    if text == '/watchlist':
        await _cmd_watchlist(event)
        return

    if text.startswith('/watch ') and not text.startswith('/watchlist'):
        name = text[len('/watch '):].strip()
        if not name:
            await event.reply("사용법: /watch 종목명")
            return
        _, msg = add_to_watchlist(name)
        await event.reply(msg)
        return

    if text.startswith('/unwatch ') or text == '/unwatch':
        name = text[len('/unwatch '):].strip() if text != '/unwatch' else ''
        if not name:
            await event.reply("사용법: /unwatch 종목명")
            return
        _, msg = remove_from_watchlist(name)
        await event.reply(msg)
        return

    if text == '/keywords':
        msg = "🔑 **DART 공시 알림 키워드**\n\n"
        for kw in DART_ALERT_KEYWORDS:
            msg += f"• {kw}\n"
        msg += f"\n총 {len(DART_ALERT_KEYWORDS)}개 키워드"
        await event.reply(msg)
        return

    # ── 채널 관리 ────────────────────────────────────────────────────────────
    if text.startswith('/채널추가'):
        await _cmd_channel_add(event, text)
        return

    if text.startswith('/채널삭제'):
        await _cmd_channel_remove(event, text)
        return

    if text == '/채널목록':
        await _cmd_channel_list(event)
        return

    # ── 도움말 ──────────────────────────────────────────────────────────────
    if text in ('/help', '/도움말'):
        await event.reply(_HELP_TEXT)
        return

    # ── 봇 채팅창: PDF 첨부 직접 분석 ────────────────────────────────────────
    if event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        await _cmd_pdf(event, text, loop)
        return

    # ── 봇 채팅창: URL 직접 요약 ─────────────────────────────────────────────
    if text and not text.startswith('/'):
        await _cmd_url(event, text, loop)


# ==========================================
# 명령어 처리 함수
# ==========================================
async def _cmd_finance(event, comp: str, loop):
    """`/재무` — DART 재무 분석 + 주가 + 차트."""
    await event.reply(f"📊 **{comp}** 분석 중... (주가 조회 + DART 재무 + 차트 생성)")

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
        path_d   = os.path.join(CHARTS_DIR, f"{comp}_daily.png")
        path_w   = os.path.join(CHARTS_DIR, f"{comp}_weekly.png")
        path_fin = os.path.join(CHARTS_DIR, f"{comp}_financials.png")
        await loop.run_in_executor(_executor, _draw_chart_kr, ticker, comp, path_d, path_w)
        await loop.run_in_executor(_executor, _draw_chart_financials, ticker, comp, path_fin)
        charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]

    header = f"📈 **[{comp} 투자 분석 리포트]**\n\n"
    if price_text:
        header += price_text + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
    await reply_chunked(event, header + fin_text)

    if charts:
        await bot_client.send_file(MY_TELEGRAM_ID, charts)
        for p in charts:
            try:
                os.remove(p)
            except OSError as e:
                log.debug("차트 파일 삭제 실패 %s: %s", p, e)


async def _cmd_us(event, query: str, loop):
    """`/us` — 미국 종목 리포트 + 차트."""
    await event.reply(f"🇺🇸 **{query}** 미국 종목 조회 중...")

    ticker      = US_STOCK_MAP.get(query.lower(), US_STOCK_MAP.get(query, query.upper()))
    path_d      = os.path.join(CHARTS_DIR, f"US_{ticker}_daily.png")
    path_w      = os.path.join(CHARTS_DIR, f"US_{ticker}_weekly.png")
    path_fin    = os.path.join(CHARTS_DIR, f"US_{ticker}_financials.png")
    report_task = loop.run_in_executor(_executor, get_us_report_text, query)
    chart_task  = loop.run_in_executor(_executor, _draw_chart_us, query, path_d, path_w)

    report_text = await report_task
    await chart_task
    await loop.run_in_executor(_executor, _draw_chart_financials, ticker, query, path_fin)

    await reply_chunked(event, f"📊 **[미국 종목 리포트]**\n\n{report_text}")

    charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]
    if charts:
        await bot_client.send_file(MY_TELEGRAM_ID, charts)
        for p in charts:
            try:
                os.remove(p)
            except OSError as e:
                log.debug("차트 파일 삭제 실패 %s: %s", p, e)


async def _cmd_dart_filings(event, comp: str, loop):
    """`/공시` — 최근 3건 공시. 조회 범위는 최근 90일 기준 동적 계산."""
    start_dt = date.today().replace(month=1, day=1).strftime('%Y-%m-%d')
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


async def _cmd_watchlist(event):
    stocks = load_watchlist()
    if not stocks:
        await event.reply("📋 관심종목이 비어 있습니다.\n\n/watch 삼성전자  →  추가")
        return
    msg = f"📋 **관심종목 ({len(stocks)}종목)**\n\n"
    for i, s in enumerate(stocks, 1):
        msg += f"{i}. {s}\n"
    msg += "\n/watch 종목명  →  추가\n/unwatch 종목명  →  삭제"
    await event.reply(msg)


async def _cmd_channel_add(event, text: str):
    ch = text.replace('/채널추가', '').strip()
    if not ch:
        await event.reply("사용법: /채널추가 @채널유저네임")
        return
    if not ch.startswith('@'):
        ch = '@' + ch
    channels = load_channels()
    if ch in channels:
        await event.reply(f"'{ch}'은(는) 이미 모니터링 중입니다.")
        return
    channels.append(ch)
    save_channels(channels)
    await update_channel_handler(channels)
    await event.reply(
        f"✅ '{ch}' 모니터링 채널에 추가됐습니다. (총 {len(channels)}개)\n\n"
        "⚠️ user_client 계정이 해당 채널에 가입되어 있어야 합니다."
    )


async def _cmd_channel_remove(event, text: str):
    ch = text.replace('/채널삭제', '').strip()
    if not ch:
        await event.reply("사용법: /채널삭제 @채널유저네임")
        return
    if not ch.startswith('@'):
        ch = '@' + ch
    channels = load_channels()
    if ch not in channels:
        await event.reply(f"'{ch}'은(는) 모니터링 목록에 없습니다.")
        return
    channels.remove(ch)
    save_channels(channels)
    await update_channel_handler(channels)
    await event.reply(f"🗑️ '{ch}' 모니터링 채널에서 삭제됐습니다. (총 {len(channels)}개)")


async def _cmd_channel_list(event):
    channels = load_channels()
    if not channels:
        await event.reply("📡 모니터링 채널이 없습니다.\n\n/채널추가 @채널유저네임  →  추가")
        return
    msg = f"📡 **모니터링 채널 ({len(channels)}개)**\n\n"
    for i, ch in enumerate(channels, 1):
        msg += f"{i}. {ch}\n"
    msg += "\n/채널추가 @유저네임  →  추가\n/채널삭제 @유저네임  →  삭제"
    await event.reply(msg)


async def _cmd_pdf(event, text: str, loop):
    """봇 채팅 PDF 첨부 → 분석. analyze_pdf가 경로 검증 + 정리 책임."""
    file_path = await event.download_media(file=DOWNLOAD_DIR)
    caption   = (text or '').strip() or '(제목 없음)'

    if not file_path:
        await event.reply("⚠️ PDF 다운로드에 실패했습니다.")
        return

    filename = os.path.basename(file_path)
    await event.reply("📄 **PDF 분석 중...**\n⏳ Gemini가 리포트를 읽고 있습니다...")
    try:
        answer = await analyze_pdf(file_path, _executor)
        await event.reply(
            f"📊 **[PDF 리포트 분석]**\n_{caption}_\n\n{answer}\n\n📎 파일명: {filename}"
        )
    except Exception as e:
        log.warning("PDF 분석 실패 (%s): %s", file_path, e)
        await event.reply("⚠️ PDF 분석 중 오류가 발생했습니다.")


async def _cmd_url(event, text: str, loop):
    """봇 채팅 URL → YouTube 자막 또는 웹 본문 요약."""
    urls = re.findall(r'https?://[^\s]+', text)
    if not urls:
        return
    url = urls[0]

    if re.search(r'(youtube\.com/watch|youtu\.be/)', url):
        vid_id = extract_youtube_id(url)
        if not vid_id:
            await event.reply("⚠️ YouTube 영상 ID를 인식할 수 없습니다.")
            return
        await event.reply("▶️ **유튜브 자막 추출 중...**")
        try:
            segments = await loop.run_in_executor(
                _executor,
                lambda: YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
            )
            transcript = ' '.join(s['text'] for s in segments)
            res = await loop.run_in_executor(
                _executor,
                lambda: generate_with_retry([LINK_PROMPT + "\n\n" + _wrap_external(transcript)])
            )
            await event.reply(f"▶️ **[유튜브 인사이트 요약]**\n\n{res.text}\n\n📎 원문: {url}")
        except Exception as e:
            log.warning("YouTube 자막/요약 실패 (%s): %s", url, e)
            await event.reply(f"⚠️ 자막을 가져올 수 없습니다.\n📎 원문: {url}")
        return

    await event.reply("🌐 **링크 분석 중...**")
    page_text = await loop.run_in_executor(_executor, fetch_webpage_text, url)
    if not page_text:
        await event.reply(f"⚠️ 해당 링크에서 내용을 가져오지 못했습니다.\n📎 {url}")
        return
    try:
        res = await loop.run_in_executor(
            _executor,
            lambda: generate_with_retry([LINK_PROMPT + "\n\n" + _wrap_external(page_text)])
        )
        await event.reply(f"🌐 **[네모 봇 인사이트 요약]**\n\n{res.text}\n\n📎 원문: {url}")
    except Exception as e:
        log.warning("URL 요약 실패 (%s): %s", url, e)
        await event.reply("⚠️ 요약 중 오류가 발생했습니다.")


_HELP_TEXT = (
    "📖 **네모 봇 명령어 도움말**\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "📊 **주식 조회**\n"
    "  `/report <종목명>` — 국내 종목 **종합 리포트** (추천)\n"
    "    └ 기업개요 · 실시간주가 · 최근공시 · 분기실적 · 증권사리포트\n"
    "    └ 차트 6종: 분기손익 · TTM매출 · TTM영업이익 · 일봉 · 주봉 · 외국인순매수\n"
    "  `/재무 <종목명>` — 국내 종목 DART 재무 분석 + 주가 + 차트\n"
    "  `/공시 <종목명>` — 국내 종목 최근 주요 공시 3건\n"
    "  `/us <종목명/티커>` — 미국 종목 주가·밸류에이션 + 차트\n"
    "    예) `/us NVDA`, `/us 엔비디아`\n\n"
    "👀 **관심종목 & DART 알림**\n"
    "  `/watch <종목명>` — 관심종목 추가\n"
    "  `/unwatch <종목명>` — 관심종목 삭제\n"
    "  `/watchlist` — 관심종목 전체 목록\n"
    "  `/keywords` — DART 공시 알림 키워드 확인\n"
    "    ※ 평일 09:00~18:00 매 30분 자동 감지\n\n"
    "📡 **채널 모니터링 관리** _(현재 비활성)_\n"
    "  `/채널추가 @유저네임` — 모니터링 채널 추가\n"
    "  `/채널삭제 @유저네임` — 모니터링 채널 삭제\n"
    "  `/채널목록` — 현재 모니터링 중인 채널 목록\n\n"
    "📈 **시황 브리핑**\n"
    "  `/시황` — 미국 3대 지수·원자재·금리·BTC 시황 즉시 조회\n"
    "    ※ 매일 07:00 자동 발송\n"
    "  `/섹터` — 국내 섹터별 등락률·핵심 이슈 브리핑 즉시 조회\n"
    "    ※ 평일 10/12/14/16시(:05) 자동 발송\n\n"
    "🏠 **생활 브리핑**\n"
    "  `/장보기` — 주간 건강 식단 + 장보기 리스트\n"
    "  `/나들이` — 아이와 가기 좋은 나들이 장소 추천\n"
    "    ※ 금 09:00 장보기 / 목 18:00 나들이 자동 전송\n\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🤖 **자동 기능**\n"
    "  • 모니터링 채널에 링크 포함 메시지 → AI 핵심 요약 _(현재 비활성)_\n"
    "  • YouTube 링크 → 자막 기반 요약\n"
    "  • PDF 첨부 → 주식 리포트 자동 분석\n\n"
    "📨 **봇에게 직접 보내기**\n"
    "  • 기사/블로그 링크를 그냥 붙여넣기 → AI 요약\n"
    "  • YouTube 링크 → 자막 기반 요약\n"
    "  • PDF 파일 첨부 → 리포트 분석"
)


# ==========================================
# 메인 가동 루틴
# ==========================================
_START_RETRIES = 5
_START_RETRY_DELAY = 2


async def main():
    """봇 진입점.

    1) MY_TELEGRAM_ID 환경변수 검증
    2) user_client + bot_client 로그인 (재시도)
    3) 채널 모니터링 핸들러 등록
    4) APScheduler 등록 (max_instances=1, coalesce=True, 시간 충돌 회피)
    5) 시작 알림 발송 후 두 클라이언트를 asyncio.gather로 유지
    """
    if not MY_TELEGRAM_ID:
        raise RuntimeError("MY_TELEGRAM_ID 환경변수가 설정되지 않았습니다. config 또는 .env를 확인하세요.")

    for attempt in range(1, _START_RETRIES + 1):
        try:
            await user_client.start()
            await bot_client.start(bot_token=TELEGRAM_BOT_TOKEN)
            break
        except Exception as exc:
            if attempt == _START_RETRIES:
                raise
            log.warning("클라이언트 시작 실패 (%d/%d): %s — %d초 후 재시도",
                        attempt, _START_RETRIES, exc, _START_RETRY_DELAY)
            await asyncio.sleep(_START_RETRY_DELAY)

    if CHANNEL_SUMMARY_ENABLED:
        init_channels = load_channels()
        if init_channels:
            user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=init_channels))
            log.info("채널 모니터링 등록 완료 (%d개)", len(init_channels))
        else:
            log.info("모니터링 채널 미설정 — /채널추가 @유저네임 으로 추가하세요")
    else:
        log.info("채널 요약 서비스 비활성 (CHANNEL_SUMMARY_ENABLED=true 로 활성화)")

    # 모든 스케줄 job 공통 옵션: 동시 실행 1건만, 누락된 트리거는 합쳐서 1회 실행, 5분 grace.
    job_defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul", job_defaults=job_defaults)

    scheduler.add_job(send_weekly_info, 'cron', day_of_week='fri', hour=9,  minute=0, args=['shop'])
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='thu', hour=18, minute=0, args=['out'])
    scheduler.add_job(send_us_morning,  'cron', hour=7, minute=0)

    # 섹터 브리핑을 :05로 옮겨 DART 공시 감지(`*/30` → :00/:30)와 시간 충돌을 피한다.
    for hh in (10, 12, 14, 16):
        scheduler.add_job(send_kr_sector_briefing, 'cron', day_of_week='mon-fri', hour=hh, minute=5)

    scheduler.add_job(
        check_dart_watchlist, 'cron',
        day_of_week='mon-fri', hour='9-18', minute='*/30',
    )
    scheduler.start()

    await bot_client.send_message(MY_TELEGRAM_ID,
        "🚀 **네모 봇 올인원 엔진 정상 가동!**\n\n"
        "🔹 **종합리포트**: /report 삼성전자\n"
        "🔹 국내: /재무 삼성전자, /공시 삼성전자\n"
        "🔹 미국: /us NVDA, /us 엔비디아\n"
        "🔹 관심종목: /watch 종목명, /unwatch 종목명, /watchlist\n"
        "🔹 시황: /시황 (매일 07:00 자동 발송)\n"
        "🔹 섹터: /섹터 (평일 10/12/14/16시 자동 발송)\n"
        "🔹 생활: /장보기, /나들이\n\n"
        "📖 전체 명령어 보기: /help"
    )

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
