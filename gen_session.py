"""
gen_session.py — Telethon StringSession 문자열 생성 도구
=========================================================
실행 방법:
    python3 gen_session.py

실행하면 전화번호 / 인증코드를 입력하라는 프롬프트가 뜹니다.
로그인 성공 후 두 개의 세션 문자열이 출력됩니다:

    TELEGRAM_USER_SESSION=1BQANOTEuJ8...
    TELEGRAM_BOT_SESSION=1BQANOTEuJ8...   (봇은 동일 계정 사용)

출력된 값을 .env 에 붙여넣기 하세요.
이후에는 user_session.session / bot_session.session 파일이 필요 없습니다.
"""

import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()

API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")


async def gen():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_str = client.session.save()
    print("\n아래 두 줄을 .env 에 추가하세요:\n")
    print(f"TELEGRAM_USER_SESSION={session_str}")
    print(f"TELEGRAM_BOT_SESSION={session_str}")
    print("\n※ bot_client 는 bot_token 으로 로그인하므로 같은 문자열을 공유해도 됩니다.")


if __name__ == "__main__":
    asyncio.run(gen())
