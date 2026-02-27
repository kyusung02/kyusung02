"""
네모 봇 (Nemo Bot) - 텔레그램 올인원 자동화 봇
=================================================
이 봇은 다음 기능을 하나의 스크립트로 통합합니다:

1. 채널 모니터링 & 자동 요약
   - 지정된 텔레그램 채널에 새 메시지가 올라오면
   - URL을 추출하고 해당 웹페이지 본문을 가져와
   - Gemini AI로 핵심 3줄 요약 + 시사점을 생성 후 전송

2. 재무 데이터 분석 (/재무 <종목명>)
   - OpenDartReader로 DART 공시 재무제표를 조회
   - 매출액·영업이익·당기순이익을 Gemini에 전달해 투자 분석 리포트 생성

3. DART 공시 조회 (/공시 <종목명>)
   - 해당 종목의 최근 주요 공시 3건을 링크와 함께 제공

4. 주간 생활 브리핑 (자동 스케줄)
   - 금요일 09:00 → /장보기 : 저당/건강 주말 식단 + 장보기 리스트
   - 목요일 18:00 → /나들이 : 41개월 아이와 가기 좋은 서울/경기 나들이 추천

사용 기술 스택:
  - Telethon  : 텔레그램 클라이언트 & 봇 API
  - Google Gemini (gemini-2.0-flash-001) : AI 요약/분석
  - OpenDartReader : 금감원 DART 재무 데이터
  - APScheduler : 비동기 주기 작업 스케줄러
  - Pandas : 재무 데이터프레임 처리
"""

import asyncio
import os
import logging
import re
import json
import pandas as pd
from datetime import datetime
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument
import urllib.request
import urllib.parse
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import OpenDartReader

# ==========================================
# 1. 로깅 및 기본 설정
# ==========================================
# 실행 시각·레벨·메시지를 포함한 표준 로그 포맷 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# config.py 에서 민감 정보(API 키, 채널 목록 등)를 불러옴
# - TELEGRAM_API_ID / TELEGRAM_API_HASH : Telethon 인증용
# - TELEGRAM_BOT_TOKEN                  : 봇 클라이언트 로그인용
# - MY_TELEGRAM_ID                      : 메시지를 받을 내 계정 ID
# - GEMINI_API_KEY                      : Google Gemini API 인증
# - WATCH_CHANNELS                      : 자동 요약할 채널 목록
# - DOWNLOAD_DIR                        : 파일 저장 디렉터리
# - DART_API_KEY                        : 금감원 DART API 인증
from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
    MY_TELEGRAM_ID, GEMINI_API_KEY, WATCH_CHANNELS,
    DOWNLOAD_DIR, DART_API_KEY,
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Gemini 및 DART 클라이언트 초기화
client = genai.Client(api_key=GEMINI_API_KEY)
dart = OpenDartReader(DART_API_KEY)

# ==========================================
# 2. AI 프롬프트 정의
# ==========================================
# 각 기능별로 Gemini에 전달할 지시문(system prompt)을 상수로 관리합니다.

# 링크 요약: 웹페이지 본문을 받아 투자 관점 요약 + 시사점 출력
LINK_PROMPT = """당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.
제공된 뉴스/기사 내용을 바탕으로 아래 형식으로 정리하세요.

[네모 봇 인사이트]
■ 핵심 요약 (3줄)
①
②
③

■ 투자 시사점
- 해당 이슈가 업종/기업 실적에 미치는 영향 (긍정/부정/중립)
- 주목해야 할 수혜주 또는 피해주가 있다면 언급

■ 리스크 요인
- 이 뉴스와 관련하여 투자자가 주의해야 할 불확실성 또는 하방 리스크

간결하고 객관적인 팩트 중심으로 서술하세요."""

# 재무 분석: DART 재무 수치를 받아 성장성·수익성·건전성 분석 + 투자 의견 제공
FINANCE_PROMPT = """당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.
아래 제공된 DART 공시 재무 데이터를 분석하여 기관 투자자 대상 리서치 리포트 형식으로 작성하세요.

[재무 트렌드 분석 리포트]

1. 매출 성장성 (Revenue Growth)
   - 전기 대비 매출액 증가율(YoY) 계산 및 성장 동력 해석
   - 성장이 일회성인지 구조적 트렌드인지 판단

2. 수익성 분석 (Profitability)
   - 영업이익률(OPM) 및 순이익률(NPM) 산출
   - 비용 구조 변화 및 레버리지 효과 평가

3. 재무 건전성 (Financial Health)
   - 이익의 질(earning quality): 영업이익과 순이익 gap 분석
   - 특이사항(일회성 이익·손실, 지분법 효과 등) 언급

4. 투자 의견 (Investment View)
   - 종합 평가: 매수(BUY) / 중립(HOLD) / 매도(SELL) 중 하나로 의견 제시
   - 핵심 투자 포인트 2~3가지
   - 주요 모니터링 지표 (다음 분기 체크포인트)

5. 리스크 요인 (Key Risks)
   - 실적 전망을 훼손할 수 있는 매크로·업황·기업 고유 리스크 2~3가지

수치 기반의 객관적 분석을 원칙으로 하며, 단정적 표현 대신 확률적·조건부 표현을 사용하세요."""

# PDF 리포트 분석: 업로드된 주식 리서치 리포트를 전문 애널리스트 관점에서 요약
PDF_PROMPT = """당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.
첨부된 주식 리서치 리포트/투자 보고서를 분석하여 아래 형식으로 정리하세요.

[PDF 리포트 분석]

■ 리포트 개요
- 발행사 / 애널리스트 / 발행일
- 분석 대상 종목 및 목표주가·투자의견 (있을 경우)

■ 핵심 투자 포인트 (3가지)
①
②
③

■ 실적 전망 요약
- 주요 재무 추정치 (매출·영업이익 등) 및 근거

■ 밸류에이션 분석
- 적용 방법론 (PER, PBR, DCF 등) 및 목표주가 산출 근거

■ 리스크 요인
- 투자 의견을 훼손할 수 있는 주요 리스크 2~3가지

■ 총평
- 기존 컨센서스 대비 차별화 포인트 및 주목 이유 한 줄 요약

객관적이고 간결하게 핵심만 서술하세요."""

# 주간 장보기: 41개월 여아 포함 3인 가족 기준 저당/건강 식단 + 장보기 목록 생성
SHOPPING_PROMPT = "41개월 여아가 있는 3인 가족을 위한 주말 저당/건강 식단과 장보기 리스트를 짜주세요."

# 주간 나들이: 아이와 함께할 수 있는 서울/경기 나들이 장소 추천
OUTING_PROMPT = "41개월 아이와 가기 좋은 서울/경기 나들이 장소를 추천하세요."

# ==========================================
# 3. 재무 분석 함수
# ==========================================
async def get_finance_summary(company):
    """
    종목명을 입력받아 DART 재무제표를 조회하고 Gemini로 분석 리포트를 생성합니다.

    Args:
        company (str): 조회할 종목명 (예: "삼성전자")

    Returns:
        str: Gemini가 생성한 재무 분석 텍스트 또는 오류 메시지

    동작 흐름:
        1) 2024년 재무제표 조회 → 없으면 2023년으로 자동 fallback
        2) 매출액·영업이익·당기순이익 행만 필터링
        3) 필터링된 수치를 FINANCE_PROMPT와 함께 Gemini에 전달
        4) 응답 텍스트 반환
    """
    try:
        # 수정 포인트 1: 함수명 fin_stat_all → finstate_all (패키지 업데이트 반영)
        # 수정 포인트 2: 2024년 데이터를 먼저 찾고 없으면 2023년으로 자동 전환
        year = 2024
        df = dart.finstate_all(company, year)
        if df is None or df.empty:
            year = 2023
            df = dart.finstate_all(company, year)

        if df is None or df.empty:
            return f"❌ '{company}'의 재무 데이터를 찾을 수 없습니다. (종목명/상장여부 확인)"

        # 핵심 계정(매출액, 영업이익, 당기순이익)만 필터링해 프롬프트 토큰 절약
        essential = df[df['account_nm'].str.contains('매출액|영업이익|당기순이익', na=False)]
        # 당기(thsstrm) vs 전기(frmtrm) 비교가 가능한 형태로 문자열 변환
        # DART API 컬럼명: thstrm_amount(당기), thstrm_add_amount(당기누적), frmtrm_amount(전기)
        # 어느 컬럼이 실제로 존재하는지 확인 후 사용
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

        response = client.models.generate_content(
            model='gemini-2.0-flash-001',
            contents=[FINANCE_PROMPT + f"\n\n종목: {company}\n데이터:\n{data_str}"]
        )
        source_note = f"\n\n📌 출처: DART 전자공시시스템 ({year}년 재무제표)"
        return response.text + source_note
    except Exception as e:
        return f"⚠️ 분석 중 오류가 발생했습니다: {e}"


def fetch_webpage_text(url):
    """
    URL에서 웹페이지 본문 텍스트를 추출합니다.

    Args:
        url (str): 크롤링할 대상 URL

    Returns:
        str | None: 정제된 텍스트(최대 5000자) 또는 실패 시 None

    동작 흐름:
        1) Mozilla User-Agent를 설정해 차단 우회
        2) HTML 태그 제거 후 연속 공백을 단일 공백으로 정규화
        3) 앞 5000자만 반환해 Gemini 입력 토큰 제한에 대응
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
        # HTML 태그 제거 → 공백 정규화 → 5000자 제한
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()[:5000]
    except:
        return None

# ==========================================
# 4. 텔레그램 클라이언트 초기화
# ==========================================
# user_client : 일반 사용자 계정으로 채널 메시지를 수신(모니터링)
# bot_client  : 봇 토큰으로 인증해 나에게 메시지를 발송
user_client = TelegramClient("user_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
bot_client = TelegramClient("bot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)


async def send_weekly_info(mode):
    """
    스케줄러가 호출하는 주간 정보 발송 함수입니다.

    Args:
        mode (str): 'shop' → 장보기 리스트, 그 외 → 나들이 추천

    동작:
        해당 프롬프트로 Gemini 응답을 생성해 내 텔레그램 계정으로 전송합니다.
    """
    prompt = SHOPPING_PROMPT if mode == 'shop' else OUTING_PROMPT
    response = client.models.generate_content(model='gemini-2.0-flash-001', contents=[prompt])
    await bot_client.send_message(MY_TELEGRAM_ID, response.text)

# ==========================================
# 5. 이벤트 핸들러
# ==========================================

def extract_youtube_id(url):
    """유튜브 URL에서 영상 ID를 추출합니다."""
    match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', url)
    return match.group(1) if match else None


@user_client.on(events.NewMessage(chats=WATCH_CHANNELS))
async def on_channel_msg(event):
    """
    WATCH_CHANNELS 에 등록된 채널에 새 메시지가 올라올 때 자동 실행됩니다.

    동작 흐름:
        [유튜브] YouTube URL 감지 → 자막 추출 → Gemini 요약 → 원문 링크 첨부
        [기사/블로그] 일반 URL 감지 → 웹 크롤링 → Gemini 요약 → 원문 링크 첨부
        [PDF] 문서 첨부 감지 → 파일 다운로드 → Gemini Files API 업로드 → 리포트 분석 → 파일 삭제
    """
    chat = await event.get_chat()
    source_name = chat.title if hasattr(chat, 'title') else "정보 채널"

    urls = re.findall(r'https?://[^\s]+', event.text or "")

    # ── 유튜브 ──────────────────────────────────────────
    if urls and re.search(r'(youtube\.com/watch|youtu\.be/)', urls[0]):
        yt_url = urls[0]
        vid_id = extract_youtube_id(yt_url)
        if vid_id:
            try:
                segments = YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
                transcript = ' '.join(s['text'] for s in segments)[:5000]
                res = client.models.generate_content(
                    model='gemini-2.0-flash-001',
                    contents=[LINK_PROMPT + f"\n내용: {transcript}"]
                )
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"▶️ **[유튜브 인사이트 요약]**\n📡 채널: {source_name}\n\n{res.text}\n\n📎 원문: {yt_url}"
                )
            except Exception as e:
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"▶️ **[유튜브]** 자막을 가져올 수 없습니다 ({e})\n📡 채널: {source_name}\n📎 원문: {yt_url}"
                )
        return

    # ── 기사 / 블로그 ────────────────────────────────────
    if urls:
        text = fetch_webpage_text(urls[0])
        if text:
            res = client.models.generate_content(
                model='gemini-2.0-flash-001',
                contents=[LINK_PROMPT + f"\n내용: {text}"]
            )
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"🌐 **[네모 봇 인사이트 요약]**\n📡 채널: {source_name}\n\n{res.text}\n\n📎 원문: {urls[0]}"
            )
        return

    # ── PDF (문서 첨부) ──────────────────────────────────
    if event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        file_path = await event.download_media(file=DOWNLOAD_DIR)
        caption = (event.text or '').strip() or '(제목 없음)'
        filename = os.path.basename(file_path) if file_path else '다운로드 실패'

        if not file_path:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신 실패]**\n{caption}\n\n⚠️ 파일 다운로드에 실패했습니다."
            )
            return

        await bot_client.send_message(
            MY_TELEGRAM_ID,
            f"📄 **[PDF 분석 중...]**\n📡 채널: {source_name}\n{caption}\n\n⏳ Gemini가 리포트를 읽고 있습니다..."
        )
        try:
            # Gemini Files API에 PDF 업로드 → 멀티모달 분석
            uploaded = client.files.upload(file=file_path)
            response = client.models.generate_content(
                model='gemini-2.0-flash-001',
                contents=[uploaded, PDF_PROMPT]
            )
            # 분석 완료 후 Gemini 서버에서 파일 삭제
            client.files.delete(name=uploaded.name)
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📊 **[PDF 리포트 분석]**\n📡 채널: {source_name}\n_{caption}_\n\n{response.text}\n\n📎 파일명: {filename}"
            )
        except Exception as e:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신]**\n📡 채널: {source_name}\n{caption}\n\n⚠️ 분석 중 오류 발생: {e}\n📎 파일명: {filename}"
            )


@bot_client.on(events.NewMessage())
async def on_bot_msg(event):
    """
    봇에게 전달되는 모든 메시지를 처리합니다.
    보안을 위해 MY_TELEGRAM_ID 에서 보낸 메시지만 응답합니다.

    지원 명령어:
        /재무 <종목명>  → DART 재무 분석 리포트
        /공시 <종목명>  → 최근 주요 공시 3건 링크
        /장보기         → 주간 장보기 리스트 즉시 요청
        /나들이         → 나들이 장소 즉시 요청
    """
    # 내 계정에서 보낸 메시지만 처리 (무단 접근 차단)
    if event.chat_id != MY_TELEGRAM_ID:
        return
    text = event.text

    if text.startswith('/재무'):
        comp = text.replace('/재무', '').strip()
        await event.reply(f"📊 **{comp}**의 재무 데이터를 분석 중입니다...")
        res = await get_finance_summary(comp)
        await event.reply(f"📈 **[{comp} 재무 트렌드 분석]**\n\n{res}")

    elif text == '/장보기':
        await send_weekly_info('shop')
    elif text == '/나들이':
        await send_weekly_info('out')

    elif text.startswith('/공시'):
        comp = text.replace('/공시', '').strip()
        # 수정 포인트 3: 검색 시작일을 2025년으로 업데이트하여 최신성 확보
        reports = dart.list(comp, start='2025-01-01')
        if reports is None or reports.empty:
            return await event.reply(f"❌ '{comp}'의 최근 공시가 없습니다.")

        msg = f"📑 **[{comp} 최근 주요 공시]**\n\n"
        for _, row in reports.head(3).iterrows():
            # DART 공시 상세 링크를 접수번호(rcpNo)로 구성
            msg += f"▪️ {row['rcept_dt']} | [{row['report_nm']}](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']})\n"
        await event.reply(msg, link_preview=False)

# ==========================================
# 6. 메인 가동 루틴
# ==========================================
async def main():
    """
    봇의 진입점(entry point)입니다.

    동작 순서:
        1) user_client (일반 계정) 로그인 → 채널 모니터링 시작
        2) bot_client (봇 토큰) 로그인  → 명령어 수신 시작
        3) APScheduler 등록
           - 금요일 09:00 KST: 장보기 리스트 전송
           - 목요일 18:00 KST: 나들이 추천 전송
        4) 시작 알림 메시지 발송
        5) 두 클라이언트를 asyncio.gather 로 동시에 유지
    """
    await user_client.start()
    await bot_client.start(bot_token=TELEGRAM_BOT_TOKEN)

    # 스케줄러 설정 (목/금 정기 브리핑)
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='fri', hour=9, args=['shop'])
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='thu', hour=18, args=['out'])
    scheduler.start()

    # 봇 가동 완료 알림 + 사용 가능한 명령어 안내
    await bot_client.send_message(MY_TELEGRAM_ID, "🚀 **네모 봇 올인원 엔진 정상 가동!**\n\n🔹 명령어: /재무, /공시, /장보기, /나들이\n🔹 자동 기능: 외부 채널 링크 자동 요약 가동 중")

    # 두 클라이언트를 동시에 실행하여 채널 수신과 봇 명령 처리를 병행
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == "__main__":
    asyncio.run(main())
