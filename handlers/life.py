"""
생활 브리핑 핸들러 — 장보기 리스트 & 나들이 추천
"""
import asyncio
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from services.gemini import client, SHOPPING_PROMPT, OUTING_PROMPT


async def send_weekly_info(mode: str):
    """
    스케줄러 또는 명령어로 호출되는 주간 정보 발송 함수.
    mode='shop' → 장보기 리스트, 그 외 → 나들이 추천
    """
    prompt = SHOPPING_PROMPT if mode == 'shop' else OUTING_PROMPT
    loop   = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        _executor,
        lambda: client.models.generate_content(model='gemini-2.5-flash', contents=[prompt])
    )
    await bot_client.send_message(MY_TELEGRAM_ID, response.text)
