"""
생활 브리핑 핸들러 — 장보기 리스트 & 나들이 추천
"""
import logging
import asyncio
from datetime import date
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from services.gemini import generate_with_retry, build_shopping_prompt, build_outing_prompt

log = logging.getLogger(__name__)


async def send_weekly_info(mode: str):
    """스케줄러 또는 명령어로 호출되는 주간 정보 발송 함수.

    mode='shop' → 장보기 리스트, 그 외 → 나들이 추천.
    호출 시점의 월을 builder에 전달해 계절·제철 컨텍스트를 반영.
    """
    month  = date.today().month
    prompt = build_shopping_prompt(month) if mode == 'shop' else build_outing_prompt(month)
    loop   = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(_executor, generate_with_retry, [prompt])
    except Exception as e:
        log.warning("send_weekly_info(%s) Gemini 호출 실패: %s", mode, e)
        await bot_client.send_message(MY_TELEGRAM_ID, "⚠️ 주간 브리핑 생성에 실패했습니다.")
        return
    await bot_client.send_message(MY_TELEGRAM_ID, response.text)
