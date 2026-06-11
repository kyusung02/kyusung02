"""네모 봇 (Nemo Bot) — 텔레그램 올인원 자동화 봇"""
import os
import sys
import asyncio
import logging
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
)
from clients import user_client, bot_client, _executor
from storage import add_to_watchlist, remove_from_watchlist, load_channels, load_forward_config
from handlers.channel import (
    on_channel_msg, handle_channel_add, handle_channel_remove, handle_channel_list,
)
from handlers.forward import (
    on_forward_msg, handle_forward_target,
    handle_forward_add, handle_forward_remove, handle_forward_list,
)
from handlers.finance import handle_finance, handle_us, handle_dart_filings, handle_watchlist
from handlers.media import handle_pdf, handle_url
from handlers.dart import check_dart_watchlist
from handlers.market import send_us_morning
from handlers.sector import send_kr_sector_briefing
from handlers.semi import send_semi_briefing
from handlers.memory import send_memory_briefing
from handlers.report import handle_report
from handlers.research import send_daily_research
from handlers.life import send_weekly_info
from handlers.portfolio import (
    handle_buy, handle_sell, handle_portfolio, handle_trade, handle_fix_ticker,
)
# import handlers.portfolio 시점에 on_trade_callback 데코레이터가 bot_client 에 자동 등록됨.
from handlers.alerts import (
    handle_alert_add, handle_alert_list, handle_alert_remove, handle_alert_natural,
    check_and_notify_alerts,
)
# on_alert_callback 데코레이터도 handlers.alerts import 시점에 bot_client 에 자동 등록.
from handlers.earnings import handle_earnings, check_and_notify_imminent_earnings
from utils import channel_summary_enabled, healthcheck_enabled, ping_healthcheck

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
    # /id 는 본인 챗 제한 없이 어디서든 응답 — 비공개 채널의 숫자 ID 확인용
    # (포워딩 대상 설정 시 필요. 챗 ID 자체는 민감정보가 아님)
    if (event.text or '').strip() == '/id':
        try:
            await event.reply(f"이 채팅의 ID: `{event.chat_id}`")
        except Exception as e:
            log.warning("/id 응답 실패 (chat=%s): %s", event.chat_id, e)
        return
    if event.chat_id != MY_TELEGRAM_ID:
        return
    text = event.text or ''

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

    # ── 반도체 업황 스크리닝 ────────────────────────────────────────────────
    if text in ('/semi', '/반도체'):
        await event.respond("🔬 반도체 업황 스크리닝 중... (약 20~30초 소요)")
        await send_semi_briefing()
        return

    # ── 메모리 현물가 (DRAM/NAND) ───────────────────────────────────────────
    if text in ('/메모리', '/memory'):
        await event.respond("🖥️ 메모리 현물가 수집 중...")
        await send_memory_briefing()
        return

    # ── 오늘의 증권사 리포트 ────────────────────────────────────────────────
    # 주의: /리포트 startswith 보다 먼저 정확 매칭으로 잡는다.
    if text == '/오늘리포트':
        await event.respond("📄 오늘 발행 증권사 리포트 수집 중...")
        await send_daily_research()
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
        await handle_finance(event, comp)
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
        await handle_us(event, query)
        return

    # ── DART 공시 ───────────────────────────────────────────────────────────
    if text.startswith('/공시'):
        comp = text.replace('/공시', '').strip()
        if not comp:
            await event.reply("사용법: /공시 삼성전자")
            return
        await handle_dart_filings(event, comp)
        return

    # ── 포트폴리오 (보유 종목) ──────────────────────────────────────────────
    # /거래 는 Gemini 파싱 + inline button 확인. /buy /sell 은 정형 명령.
    if text.startswith('/거래 ') or text == '/거래':
        await handle_trade(event, text[len('/거래'):].strip())
        return
    if text.startswith('/buy ') or text == '/buy':
        await handle_buy(event, text[len('/buy'):].strip())
        return
    if text.startswith('/sell ') or text == '/sell':
        await handle_sell(event, text[len('/sell'):].strip())
        return
    if text == '/portfolio' or text == '/포폴':
        await handle_portfolio(event)
        return
    if text.startswith('/티커수정'):
        await handle_fix_ticker(event, text[len('/티커수정'):].strip())
        return

    # ── 가격·이벤트 알림 ────────────────────────────────────────────────────
    if text.startswith('/알림 ') or text == '/알림':
        await handle_alert_natural(event, text[len('/알림'):].strip())
        return
    if text.startswith('/alert ') or text == '/alert':
        await handle_alert_add(event, text[len('/alert'):].strip())
        return
    if text == '/alerts':
        await handle_alert_list(event)
        return
    if text.startswith('/unalert ') or text == '/unalert':
        await handle_alert_remove(event, text[len('/unalert'):].strip())
        return

    # ── 어닝 (실적 발표) ─────────────────────────────────────────────────────
    if text.startswith('/earnings ') or text == '/earnings':
        await handle_earnings(event, text[len('/earnings'):].strip())
        return
    if text.startswith('/어닝 ') or text == '/어닝':
        await handle_earnings(event, text[len('/어닝'):].strip())
        return

    # ── 관심종목 관리 ────────────────────────────────────────────────────────
    # 주의: /unwatch는 반드시 /watchlist 보다 뒤에 두지 말고, 공백 필수로 처리해
    # /unwatchlist 등의 명령이 잘못 매칭되지 않도록 한다.
    if text == '/watchlist':
        await handle_watchlist(event)
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
        await handle_channel_add(event, text)
        return

    if text.startswith('/채널삭제'):
        await handle_channel_remove(event, text)
        return

    if text == '/채널목록':
        await handle_channel_list(event)
        return

    # ── 채널 포워딩 ──────────────────────────────────────────────────────────
    if text.startswith('/포워딩추가'):
        await handle_forward_add(event, text[len('/포워딩추가'):])
        return
    if text.startswith('/포워딩삭제'):
        await handle_forward_remove(event, text[len('/포워딩삭제'):])
        return
    if text == '/포워딩목록':
        await handle_forward_list(event)
        return
    if text.startswith('/포워딩대상'):
        await handle_forward_target(event, text[len('/포워딩대상'):])
        return

    # ── 도움말 ──────────────────────────────────────────────────────────────
    if text in ('/help', '/도움말'):
        await event.reply(_HELP_TEXT)
        return

    # ── 봇 채팅창: PDF 첨부 직접 분석 ────────────────────────────────────────
    if event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        await handle_pdf(event, text)
        return

    # ── 봇 채팅창: URL 직접 요약 ─────────────────────────────────────────────
    if text and not text.startswith('/'):
        await handle_url(event, text)


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
    "💼 **포트폴리오 (보유 종목)**\n"
    "  `/거래 <자연어>` — 자연어로 매매 + 버튼 확인\n"
    "    예) `/거래 삼성전자 10주 8만원에 샀어`, `/거래 nvda 전량 정리`\n"
    "  `/buy <종목> <수량> <단가> [날짜]` — 정형 매수 (재매수 시 평단가 가중평균)\n"
    "  `/sell <종목> [수량]` — 정형 매도 (수량 생략 시 전량)\n"
    "  `/portfolio` (또는 `/포폴`) — 평가금액·수익률·종목별 비중\n"
    "  `/티커수정 <기존> <새>` — 잘못 저장된 티커 교정 (보유 내역 유지)\n\n"
    "📅 **어닝 (실적 발표)**\n"
    "  `/어닝 <종목>` 또는 `/earnings <종목>` — 어닝 히스토리(EPS 추정 vs 실제) + 다음 예정\n"
    "    예) `/어닝 NVDA`, `/어닝 엔비디아`\n"
    "    ※ 미국 종목 위주. 모닝 시황에 1주 내 보유·관심 종목 어닝 예정 자동 첨부.\n\n"
    "🔔 **가격·이벤트 알림**\n"
    "  `/알림 <자연어>` — 자연어로 등록 + 버튼 확인 (모든 타입 지원)\n"
    "    예) `/알림 삼성전자 9만원 넘으면`, `/알림 nvda 10% 빠지면 손절`,\n"
    "        `/알림 삼전 52주 신고가`, `/알림 카카오 거래량 5배 터지면`,\n"
    "        `/알림 삼성전자 과매도`\n"
    "  `/alert <종목> above|below <가격> [메모]` — 정형 가격 임계\n"
    "  `/alert <종목> +5%` 또는 `-5%` — 전일 종가 대비 급등·급락\n"
    "  `/alerts` — 활성 알림 목록  •  `/unalert <id>` — 삭제\n"
    "    ※ 매 5분 자동 체크. 트리거 시 자동 삭제(1회성).\n\n"
    "📡 **채널 모니터링 관리** _(현재 비활성)_\n"
    "  `/채널추가 @유저네임` — 모니터링 채널 추가\n"
    "  `/채널삭제 @유저네임` — 모니터링 채널 삭제\n"
    "  `/채널목록` — 현재 모니터링 중인 채널 목록\n\n"
    "📨 **채널 포워딩** (지정 채널 → 내 채널, 원문 그대로)\n"
    "  `/포워딩대상 @내채널` — 전달받을 대상 채널 설정\n"
    "  `/포워딩추가 @채널` — 포워딩할 소스 채널 추가\n"
    "  `/포워딩삭제 @채널` — 소스 채널 삭제\n"
    "  `/포워딩목록` — 포워딩 설정·상태 확인\n"
    "    ※ 포워딩 금지 채널은 복사 전송으로 자동 폴백\n\n"
    "📈 **시황 브리핑**\n"
    "  `/시황` — 미국 3대 지수·원자재·금리·BTC·SOX·SMH 시황 즉시 조회\n"
    "    ※ 매일 07:00 자동 발송\n"
    "  `/섹터` — 국내 섹터별 등락률·핵심 이슈 브리핑 즉시 조회\n"
    "    ※ 평일 10/12/14/16시(:05) 자동 발송\n"
    "  `/semi` (또는 `/반도체`) — 반도체 업황 스크리닝 (한·미 메모리·장비·빅테크 CAPEX)\n"
    "    ※ 매주 월 08:00 자동 발송\n"
    "  `/메모리` — DRAM/NAND 현물가 (DRAMeXchange) + 메모리주 프록시\n"
    "    ※ 화~금 08:00 자동 발송\n"
    "  `/오늘리포트` — 오늘 발행된 국내 증권사 리포트 모아보기 (보유·관심 종목 ⭐)\n"
    "    ※ 평일 09:00 자동 발송\n\n"
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
_START_RETRY_DELAY_SEC = 2


async def _heartbeat():
    """외부 데드맨 스위치 ping. 봇이 실제로 연결돼 있을 때만 보낸다.

    bot_client가 끊겼거나(또는 프로세스/VM이 죽으면) ping이 멈추고, 외부 모니터가
    grace 경과 후 알림을 보낸다 → OOM으로 VM이 먹통(SSH도 불가, 인스턴스는 RUNNING)인
    상황까지 감지. 블로킹 HTTP는 executor에서 돌려 이벤트 루프를 막지 않는다.
    """
    if not bot_client.is_connected():
        log.warning("healthcheck: bot_client 연결 끊김 — ping 생략(모니터가 down 감지하도록)")
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, ping_healthcheck)


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
                        attempt, _START_RETRIES, exc, _START_RETRY_DELAY_SEC)
            await asyncio.sleep(_START_RETRY_DELAY_SEC)

    if channel_summary_enabled():
        init_channels = load_channels()
        if init_channels:
            user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=init_channels))
            log.info("채널 모니터링 등록 완료 (%d개)", len(init_channels))
        else:
            log.info("모니터링 채널 미설정 — /채널추가 @유저네임 으로 추가하세요")
    else:
        log.info("채널 요약 서비스 비활성 (CHANNEL_SUMMARY_ENABLED=true 로 활성화)")

    # 채널 포워딩 — 요약 서비스와 독립. 소스+대상이 설정되어 있으면 활성.
    fwd_cfg = load_forward_config()
    if fwd_cfg["sources"] and fwd_cfg["target"] is not None:
        user_client.add_event_handler(on_forward_msg, events.NewMessage(chats=fwd_cfg["sources"]))
        log.info("채널 포워딩 등록 완료 (%d개 → %s)", len(fwd_cfg["sources"]), fwd_cfg["target"])
    else:
        log.info("채널 포워딩 미설정 — /포워딩대상 + /포워딩추가 로 활성화")

    # 모든 스케줄 job 공통 옵션: 동시 실행 1건만, 누락된 트리거는 합쳐서 1회 실행, 5분 grace.
    job_defaults = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul", job_defaults=job_defaults)

    scheduler.add_job(send_weekly_info, 'cron', day_of_week='fri', hour=9,  minute=0, args=['shop'])
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='thu', hour=18, minute=0, args=['out'])
    scheduler.add_job(send_us_morning,  'cron', hour=7, minute=0)

    # 오늘의 증권사 리포트: 평일 09:00 KST. 장 시작 시점에 리포트가 충분히 올라온 뒤 발송.
    scheduler.add_job(
        send_daily_research, 'cron',
        day_of_week='mon-fri', hour=9, minute=0,
    )

    # 섹터 브리핑을 :05로 옮겨 DART 공시 감지(`*/30` → :00/:30)와 시간 충돌을 피한다.
    for hh in (10, 12, 14, 16):
        scheduler.add_job(send_kr_sector_briefing, 'cron', day_of_week='mon-fri', hour=hh, minute=5)

    scheduler.add_job(
        check_dart_watchlist, 'cron',
        day_of_week='mon-fri', hour='9-18', minute='*/30',
    )

    # 가격·이벤트 알림: 매 5분. 활성 알림 0개면 빈 list 반환되어 즉시 종료.
    scheduler.add_job(check_and_notify_alerts, 'interval', minutes=5)

    # 어닝 임박(D-0/D-1) 푸시: 평일 08:30 KST. 보유+관심 미국 종목 한정.
    scheduler.add_job(
        check_and_notify_imminent_earnings, 'cron',
        day_of_week='mon-fri', hour=8, minute=30,
    )

    # 주간 반도체 업황 스크리닝: 월요일 08:00 KST. 주말 누적 흐름 + 한 주 시작 시점.
    # 07:00 모닝, 08:30 어닝 사이 빈 슬롯 — 셋이 30분 간격으로 차곡.
    scheduler.add_job(
        send_semi_briefing, 'cron',
        day_of_week='mon', hour=8, minute=0,
    )

    # 메모리 현물가(DRAM/NAND): 화~금 08:00 KST. 월요일은 반도체 브리핑이 커버하므로 제외.
    scheduler.add_job(
        send_memory_briefing, 'cron',
        day_of_week='tue-fri', hour=8, minute=0,
    )

    scheduler.start()

    # 외부 데드맨 스위치(healthchecks.io 등): 2분마다 ping. grace(15~20분)보다 충분히 짧게.
    # HEALTHCHECK_URL 미설정 시 비활성 — 봇 동작에 무영향.
    if healthcheck_enabled():
        scheduler.add_job(_heartbeat, 'interval', minutes=2)
        await _heartbeat()  # 시작 즉시 1회 — 모니터 'last ping' 빠른 확인용
        log.info("Healthcheck 활성 — 2분마다 외부 모니터에 ping")
    else:
        log.info("Healthcheck 비활성 (HEALTHCHECK_URL 미설정)")

    await bot_client.send_message(MY_TELEGRAM_ID,
        "🚀 **네모 봇 올인원 엔진 정상 가동!**\n\n"
        "🔹 **종합리포트**: /report 삼성전자\n"
        "🔹 국내: /재무 삼성전자, /공시 삼성전자\n"
        "🔹 미국: /us NVDA, /us 엔비디아\n"
        "🔹 관심종목: /watch 종목명, /unwatch 종목명, /watchlist\n"
        "🔹 포트폴리오: /거래, /portfolio (국내가는 네이버 시세)\n"
        "🔹 알림: /알림 (매 5분 자동 체크)\n"
        "🔹 시황: /시황 (매일 07:00 자동 발송)\n"
        "🔹 섹터: /섹터 (평일 10/12/14/16시 자동 발송)\n"
        "🔹 반도체: /semi (월요일 08:00 자동 발송)\n"
        "🔹 채널 포워딩: /포워딩대상 + /포워딩추가 (원문 그대로 전달)\n"
        "🔹 생활: /장보기, /나들이\n\n"
        "📖 전체 명령어 보기: /help"
    )

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected(),
    )


if __name__ == "__main__":
    asyncio.run(main())
