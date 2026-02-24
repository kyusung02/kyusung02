import asyncio
import os
import logging
import re
from telethon import TelegramClient, events
import urllib.request
from google import genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import OpenDartReader

# 1. 로깅 및 기본 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# 설정 로드 (config.py에서 가져옴)
from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
    MY_TELEGRAM_ID, GEMINI_API_KEY, WATCH_CHANNELS,
    DOWNLOAD_DIR, DART_API_KEY,
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
client = genai.Client(api_key=GEMINI_API_KEY)
dart = OpenDartReader(DART_API_KEY)

# ==========================================
# 🎯 [프롬프트 세팅 - 전문 퀀트 모드]
# ==========================================
LINK_PROMPT = "제공된 링크 내용을 바탕으로 핵심 요약 3줄과 주요 시사점을 [네모 봇 인사이트] 형식으로 정리하세요."
FINANCE_PROMPT = "제공된 재무 데이터를 바탕으로 매출 성장성, 수익성(OPM), 재무 건전성을 요약하고 전문적인 투자 의견을 주세요."
SHOPPING_PROMPT = "41개월 여아가 있는 3인 가족을 위한 주말 저당/건강 식단과 장보기 리스트를 짜주세요."
OUTING_PROMPT = "41개월 아이와 가기 좋은 서울/경기 나들이 장소를 추천하세요."

# ==========================================
# 🛠️ [핵심 분석 함수 - 수치 분석 최적화]
# ==========================================
async def get_finance_summary(company):
    try:
        # ✅ 수정 포인트 1: 함수명 fin_stat_all -> finstate_all (패키지 업데이트 반영)
        # ✅ 수정 포인트 2: 2024년 데이터를 먼저 찾고 없으면 2023년으로 자동 전환
        df = dart.finstate_all(company, 2024)
        if df is None or df.empty:
            df = dart.finstate_all(company, 2023)

        if df is None or df.empty:
            return f"❌ '{company}'의 재무 데이터를 찾을 수 없습니다. (종목명/상장여부 확인)"

        # 핵심 계정(매출액, 영업이익, 당기순이익) 필터링
        essential = df[df['account_nm'].str.contains('매출액|영업이익|당기순이익', na=False)]
        data_str = essential[['account_nm', 'thsstrm_amount', 'frmtrm_amount']].to_string()

        response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=[FINANCE_PROMPT + f"\n\n종목: {company}\n데이터:\n{data_str}"]
        )
        return response.text
    except Exception as e:
        return f"⚠️ 분석 중 오류가 발생했습니다: {e}"

def fetch_webpage_text(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()[:5000]
    except:
        return None

# ==========================================
# 📡 [텔레그램 핸들러 및 스케줄러]
# ==========================================
user_client = TelegramClient("user_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
bot_client = TelegramClient("bot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)

async def send_weekly_info(mode):
    prompt = SHOPPING_PROMPT if mode == 'shop' else OUTING_PROMPT
    response = client.models.generate_content(model='gemini-2.0-flash', contents=[prompt])
    await bot_client.send_message(MY_TELEGRAM_ID, response.text)

# 채널 메시지(선진짱 등) 자동 요약
if WATCH_CHANNELS:
    @user_client.on(events.NewMessage(chats=WATCH_CHANNELS))
    async def on_channel_msg(event):
        urls = re.findall(r'https?://[^\s]+', event.text or "")
        if urls:
            text = fetch_webpage_text(urls[0])
            if text:
                res = client.models.generate_content(model='gemini-2.0-flash', contents=[LINK_PROMPT + f"\n내용: {text}"])
                await bot_client.send_message(MY_TELEGRAM_ID, f"🌐 **[네모 봇 인사이트 요약]**\n{res.text}")

# 봇 명령어 처리
@bot_client.on(events.NewMessage())
async def on_bot_msg(event):
    if event.chat_id != MY_TELEGRAM_ID:
        return
    text = event.text or ""

    if text.startswith('/재무'):
        comp = text.replace('/재무', '').strip()
        if not comp:
            return await event.reply("❌ 종목명을 입력하세요. 예: /재무 삼성전자")
        await event.reply(f"📊 **{comp}**의 재무 데이터를 분석 중입니다...")
        res = await get_finance_summary(comp)
        await event.reply(f"📈 **[{comp} 재무 트렌드 분석]**\n\n{res}")

    elif text == '/장보기':
        await send_weekly_info('shop')
    elif text == '/나들이':
        await send_weekly_info('out')

    elif text.startswith('/공시'):
        comp = text.replace('/공시', '').strip()
        if not comp:
            return await event.reply("❌ 종목명을 입력하세요. 예: /공시 삼성전자")
        # ✅ 수정 포인트 3: 검색 시작일을 2025년으로 업데이트하여 최신성 확보
        reports = dart.list(comp, start='2025-01-01')
        if reports is None or reports.empty:
            return await event.reply(f"❌ '{comp}'의 최근 공시가 없습니다.")

        msg = f"📑 **[{comp} 최근 주요 공시]**\n\n"
        for _, row in reports.head(3).iterrows():
            msg += f"▪️ {row['rcept_dt']} | [{row['report_nm']}](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']})\n"
        await event.reply(msg, link_preview=False)

# ==========================================
# 🏁 [메인 가동 루틴]
# ==========================================
async def main():
    await user_client.start()
    await bot_client.start(bot_token=TELEGRAM_BOT_TOKEN)

    # 스케줄러 설정 (목/금 정기 브리핑)
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='fri', hour=9, kwargs={'mode': 'shop'})
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='thu', hour=18, kwargs={'mode': 'out'})
    scheduler.start()

    await bot_client.send_message(MY_TELEGRAM_ID, "🚀 **네모 봇 올인원 엔진 정상 가동!**\n\n🔹 명령어: /재무, /공시, /장보기, /나들이\n🔹 자동 기능: 외부 채널 링크 자동 요약 가동 중")
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == "__main__":
    asyncio.run(main())
