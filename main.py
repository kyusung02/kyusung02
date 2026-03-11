"""
네모 봇 (Nemo Bot) — 텔레그램 올인원 자동화 봇
=================================================
기능 목록:
  1. 채널 모니터링 & 자동 요약 (링크/유튜브/PDF)
  2. 재무 데이터 분석 (/재무 <종목명>)
  3. DART 공시 조회 (/공시 <종목명>)
  4. 모닝 시황 브리핑 (/시황, 매일 07:00 자동)
  5. 주간 생활 브리핑 (/장보기, /나들이)
  6. 미국 주식 조회 (/us <종목명 또는 ticker>)
  7. 관심종목 관리 + DART 공시 자동 감지

모듈 구조:
  clients.py          — Telethon 클라이언트, ThreadPoolExecutor
  storage.py          — watchlist / channels / seen_filings JSON 관리
  utils.py            — 웹 크롤링, YouTube ID 추출
  services/gemini.py  — Gemini 클라이언트 & 프롬프트 상수
  services/stock.py   — yfinance 주가 조회, DART 종목 코드 변환
  services/chart.py   — 주가/실적/투자자 흐름 차트 생성
  handlers/channel.py — 채널 메시지 자동 요약
  handlers/dart.py    — DART 재무·공시·관심종목 감지
  handlers/market.py  — 시황 브리핑
  handlers/report.py  — 종합 리포트 (/report)
  handlers/life.py    — 장보기·나들이 브리핑
"""

from dotenv import load_dotenv
load_dotenv()

import os
import re
import uuid
import asyncio
import logging
from telethon import events
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import (
    TELEGRAM_BOT_TOKEN, MY_TELEGRAM_ID,
    DOWNLOAD_DIR, CHARTS_DIR, DART_ALERT_KEYWORDS,
)
from clients import user_client, bot_client, _executor
from storage import (
    load_watchlist, add_to_watchlist, remove_from_watchlist,
    load_channels, save_channels,
)
from services.gemini import client as gemini_client, LINK_PROMPT, PDF_PROMPT
from services.stock import get_kr_ticker, get_us_report_text, get_price_info_kr, US_STOCK_MAP
from services.chart import _draw_chart_kr, _draw_chart_us, _draw_chart_financials
from handlers.channel import on_channel_msg, update_channel_handler
from handlers.dart import (
    _get_finance_summary_sync, check_dart_watchlist,
    _get_dart_recent_filings_sync,
)
from handlers.market import send_us_morning
from handlers.sector import send_kr_sector_briefing
from handlers.report import handle_report
from handlers.life import send_weekly_info
from utils import extract_youtube_id, fetch_webpage_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CHARTS_DIR, exist_ok=True)


# ==========================================
# 봇 메시지 핸들러 (명령어 라우팅)
# ==========================================
@bot_client.on(events.NewMessage())
async def on_bot_msg(event):
    """
    봇에게 전달되는 모든 메시지를 처리합니다.
    MY_TELEGRAM_ID 에서 보낸 메시지만 응답합니다.

    지원 명령어:
        /report, /리포트 <종목명>   — 국내 종목 종합 리포트
        /재무 <종목명>              — DART 재무 분석 + 주가 + 차트
        /공시 <종목명>              — 최근 주요 공시 3건
        /us <종목명/ticker>         — 미국 종목 주가·밸류에이션 + 차트
        /watch, /unwatch, /watchlist — 관심종목 관리
        /keywords                  — DART 알림 키워드 확인
        /시황                       — 미국 시황 브리핑 즉시 요청
        /장보기, /나들이             — 주간 생활 브리핑
        /채널추가, /채널삭제, /채널목록 — 모니터링 채널 관리
        /help, /도움말              — 전체 명령어 안내
    """
    if event.chat_id != MY_TELEGRAM_ID:
        return
    text = event.text
    loop = asyncio.get_running_loop()

    # ── 시황 ────────────────────────────────────────────────────────────────
    if text == '/시황':
        await event.reply("🌅 미국 시황 데이터 수집 중...")
        await send_us_morning()

    # ── 섹터 브리핑 ──────────────────────────────────────────────────────────
    elif text == '/섹터':
        await event.respond("📊 섹터 데이터 수집 중... (약 20~30초 소요)")
        await send_kr_sector_briefing()

    # ── 종합 리포트 ─────────────────────────────────────────────────────────
    elif text.startswith('/report') or text.startswith('/리포트'):
        comp = text.replace('/report', '').replace('/리포트', '').strip()
        if not comp:
            return await event.reply("사용법: /report 삼성전자")
        await handle_report(event, comp)

    # ── 재무 분석 ───────────────────────────────────────────────────────────
    elif text.startswith('/재무'):
        comp = text.replace('/재무', '').strip()
        await event.reply(f"📊 **{comp}** 분석 중... (주가 조회 + DART 재무 + 차트 생성)")

        ticker     = get_kr_ticker(comp)
        fin_task   = loop.run_in_executor(_executor, _get_finance_summary_sync, comp)
        price_task = loop.run_in_executor(_executor, get_price_info_kr, ticker, comp) if ticker else None

        if price_task:
            fin_text, price_text = await asyncio.gather(fin_task, price_task)
        else:
            fin_text  = await fin_task
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
        full = header + fin_text
        for i in range(0, len(full), 4096):
            await event.reply(full[i:i + 4096])

        if charts:
            await bot_client.send_file(MY_TELEGRAM_ID, charts)
            for p in charts:
                try:
                    os.remove(p)
                except Exception:
                    pass

    # ── 생활 브리핑 ─────────────────────────────────────────────────────────
    elif text == '/장보기':
        await send_weekly_info('shop')
    elif text == '/나들이':
        await send_weekly_info('out')

    # ── 미국 주식 ───────────────────────────────────────────────────────────
    elif text.lower().startswith('/us '):
        query  = text[4:].strip()
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

        full = f"📊 **[미국 종목 리포트]**\n\n{report_text}"
        for i in range(0, len(full), 4096):
            await event.reply(full[i:i + 4096])

        charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]
        if charts:
            await bot_client.send_file(MY_TELEGRAM_ID, charts)
            for p in charts:
                try:
                    os.remove(p)
                except Exception:
                    pass

    # ── DART 공시 ───────────────────────────────────────────────────────────
    elif text.startswith('/공시'):
        comp    = text.replace('/공시', '').strip()
        from services.stock import dart
        reports = await loop.run_in_executor(
            _executor, lambda: dart.list(comp, start='2025-01-01')
        )
        if reports is None or reports.empty:
            return await event.reply(f"❌ '{comp}'의 최근 공시가 없습니다.")
        msg = f"📑 **[{comp} 최근 주요 공시]**\n\n"
        for _, row in reports.head(3).iterrows():
            msg += f"▪️ {row['rcept_dt']} | [{row['report_nm']}](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']})\n"
        await event.reply(msg, link_preview=False)

    # ── 관심종목 관리 ────────────────────────────────────────────────────────
    elif text.startswith('/watch ') and not text.startswith('/watchlist'):
        name = text[7:].strip()
        if not name:
            return await event.reply("사용법: /watch 종목명")
        ok, msg = add_to_watchlist(name)
        await event.reply(msg)

    elif text.startswith('/unwatch'):
        name = text.replace('/unwatch', '').strip()
        if not name:
            return await event.reply("사용법: /unwatch 종목명")
        ok, msg = remove_from_watchlist(name)
        await event.reply(msg)

    elif text == '/watchlist':
        stocks = load_watchlist()
        if not stocks:
            return await event.reply("📋 관심종목이 비어 있습니다.\n\n/watch 삼성전자  →  추가")
        msg = f"📋 **관심종목 ({len(stocks)}종목)**\n\n"
        for i, s in enumerate(stocks, 1):
            msg += f"{i}. {s}\n"
        msg += "\n/watch 종목명  →  추가\n/unwatch 종목명  →  삭제"
        await event.reply(msg)

    elif text == '/keywords':
        msg = "🔑 **DART 공시 알림 키워드**\n\n"
        for kw in DART_ALERT_KEYWORDS:
            msg += f"• {kw}\n"
        msg += f"\n총 {len(DART_ALERT_KEYWORDS)}개 키워드"
        await event.reply(msg)

    # ── 채널 관리 ────────────────────────────────────────────────────────────
    elif text.startswith('/채널추가'):
        ch = text.replace('/채널추가', '').strip()
        if not ch:
            return await event.reply("사용법: /채널추가 @채널유저네임")
        if not ch.startswith('@'):
            ch = '@' + ch
        channels = load_channels()
        if ch in channels:
            return await event.reply(f"'{ch}'은(는) 이미 모니터링 중입니다.")
        channels.append(ch)
        save_channels(channels)
        await update_channel_handler(channels)
        await event.reply(f"✅ '{ch}' 모니터링 채널에 추가됐습니다. (총 {len(channels)}개)\n\n⚠️ user_client 계정이 해당 채널에 가입되어 있어야 합니다.")

    elif text.startswith('/채널삭제'):
        ch = text.replace('/채널삭제', '').strip()
        if not ch:
            return await event.reply("사용법: /채널삭제 @채널유저네임")
        if not ch.startswith('@'):
            ch = '@' + ch
        channels = load_channels()
        if ch not in channels:
            return await event.reply(f"'{ch}'은(는) 모니터링 목록에 없습니다.")
        channels.remove(ch)
        save_channels(channels)
        await update_channel_handler(channels)
        await event.reply(f"🗑️ '{ch}' 모니터링 채널에서 삭제됐습니다. (총 {len(channels)}개)")

    elif text == '/채널목록':
        channels = load_channels()
        if not channels:
            return await event.reply("📡 모니터링 채널이 없습니다.\n\n/채널추가 @채널유저네임  →  추가")
        msg = f"📡 **모니터링 채널 ({len(channels)}개)**\n\n"
        for i, ch in enumerate(channels, 1):
            msg += f"{i}. {ch}\n"
        msg += "\n/채널추가 @유저네임  →  추가\n/채널삭제 @유저네임  →  삭제"
        await event.reply(msg)

    # ── 도움말 ──────────────────────────────────────────────────────────────
    elif text in ('/help', '/도움말'):
        msg = (
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
            "📡 **채널 모니터링 관리**\n"
            "  `/채널추가 @유저네임` — 모니터링 채널 추가\n"
            "  `/채널삭제 @유저네임` — 모니터링 채널 삭제\n"
            "  `/채널목록` — 현재 모니터링 중인 채널 목록\n\n"
            "📈 **시황 브리핑**\n"
            "  `/시황` — 미국 3대 지수·원자재·금리·BTC 시황 즉시 조회\n"
            "    ※ 매일 07:00 자동 발송\n\n"
            "🏠 **생활 브리핑**\n"
            "  `/장보기` — 주간 건강 식단 + 장보기 리스트\n"
            "  `/나들이` — 아이와 가기 좋은 나들이 장소 추천\n"
            "    ※ 금 09:00 장보기 / 목 18:00 나들이 자동 전송\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🤖 **자동 기능**\n"
            "  • 모니터링 채널에 링크 포함 메시지 → AI 핵심 요약\n"
            "  • YouTube 링크 → 자막 기반 요약\n"
            "  • PDF 첨부 → 주식 리포트 자동 분석\n\n"
            "📨 **봇에게 직접 보내기**\n"
            "  • 기사/블로그 링크를 그냥 붙여넣기 → AI 요약\n"
            "  • YouTube 링크 → 자막 기반 요약\n"
            "  • PDF 파일 첨부 → 리포트 분석"
        )
        await event.reply(msg)

    # ── 봇 채팅창: PDF 첨부 직접 분석 ────────────────────────────────────────
    elif event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        file_path = await event.download_media(file=DOWNLOAD_DIR)
        caption   = (text or '').strip() or '(제목 없음)'
        filename  = os.path.basename(file_path) if file_path else '다운로드 실패'

        if not file_path:
            await event.reply("⚠️ PDF 다운로드에 실패했습니다.")
            return

        try:
            file_path.encode('ascii')
        except UnicodeEncodeError:
            safe_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.pdf")
            os.rename(file_path, safe_path)
            file_path = safe_path

        await event.reply("📄 **PDF 분석 중...**\n⏳ Gemini가 리포트를 읽고 있습니다...")
        uploaded = None
        try:
            uploaded = await loop.run_in_executor(
                _executor, lambda: gemini_client.files.upload(file=file_path)
            )
            response = await loop.run_in_executor(
                _executor,
                lambda: gemini_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[uploaded, PDF_PROMPT]
                )
            )
            await event.reply(
                f"📊 **[PDF 리포트 분석]**\n_{caption}_\n\n{response.text}\n\n📎 파일명: {filename}"
            )
        except Exception as e:
            await event.reply(f"⚠️ PDF 분석 중 오류 발생: {e}")
        finally:
            if uploaded:
                try:
                    await loop.run_in_executor(
                        _executor, lambda: gemini_client.files.delete(name=uploaded.name)
                    )
                except Exception:
                    pass
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass

    # ── 봇 채팅창: URL 직접 요약 ─────────────────────────────────────────────
    elif text and not text.startswith('/'):
        urls = re.findall(r'https?://[^\s]+', text)
        if not urls:
            return
        url = urls[0]

        if re.search(r'(youtube\.com/watch|youtu\.be/)', url):
            vid_id = extract_youtube_id(url)
            if not vid_id:
                return await event.reply("⚠️ YouTube 영상 ID를 인식할 수 없습니다.")
            await event.reply("▶️ **유튜브 자막 추출 중...**")
            try:
                from youtube_transcript_api import YouTubeTranscriptApi
                segments = await loop.run_in_executor(
                    _executor,
                    lambda: YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
                )
                transcript = ' '.join(s['text'] for s in segments)[:5000]
                res = await loop.run_in_executor(
                    _executor,
                    lambda: gemini_client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[LINK_PROMPT + f"\n내용: {transcript}"]
                    )
                )
                await event.reply(f"▶️ **[유튜브 인사이트 요약]**\n\n{res.text}\n\n📎 원문: {url}")
            except Exception as e:
                await event.reply(f"⚠️ 자막을 가져올 수 없습니다: {e}\n📎 원문: {url}")
        else:
            await event.reply("🌐 **링크 분석 중...**")
            page_text = await loop.run_in_executor(_executor, fetch_webpage_text, url)
            if not page_text:
                return await event.reply(f"⚠️ 해당 링크에서 내용을 가져오지 못했습니다.\n📎 {url}")
            _pt = page_text
            res = await loop.run_in_executor(
                _executor,
                lambda: gemini_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[LINK_PROMPT + f"\n내용: {_pt}"]
                )
            )
            await event.reply(f"🌐 **[네모 봇 인사이트 요약]**\n\n{res.text}\n\n📎 원문: {url}")


# ==========================================
# 메인 가동 루틴
# ==========================================
async def main():
    """
    봇의 진입점입니다.
      1) user_client (일반 계정) 로그인 → 채널 모니터링 핸들러 등록
      2) bot_client  (봇 토큰) 로그인  → 명령어 수신 시작
      3) APScheduler 등록 (시황/장보기/나들이/DART 공시 감지)
      4) 시작 알림 발송 후 두 클라이언트를 asyncio.gather로 유지
    """
    for attempt in range(1, 6):
        try:
            await user_client.start()
            await bot_client.start(bot_token=TELEGRAM_BOT_TOKEN)
            break
        except Exception as exc:
            if attempt == 5:
                raise
            log.warning("클라이언트 시작 실패 (%d/5): %s — 2초 후 재시도", attempt, exc)
            await asyncio.sleep(2)

    _init_channels = load_channels()
    if _init_channels:
        user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=_init_channels))
        log.info("채널 모니터링 등록 완료: %s", _init_channels)
    else:
        log.info("모니터링 채널 미설정 — /채널추가 @유저네임 으로 추가하세요")

    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='fri', hour=9,  args=['shop'])
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='thu', hour=18, args=['out'])
    scheduler.add_job(send_us_morning,  'cron', hour=7, minute=0)
    scheduler.add_job(send_kr_sector_briefing, 'cron', day_of_week='mon-fri', hour=10, minute=0)
    scheduler.add_job(send_kr_sector_briefing, 'cron', day_of_week='mon-fri', hour=12, minute=0)
    scheduler.add_job(send_kr_sector_briefing, 'cron', day_of_week='mon-fri', hour=14, minute=0)
    scheduler.add_job(send_kr_sector_briefing, 'cron', day_of_week='mon-fri', hour=16, minute=0)
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
