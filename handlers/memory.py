"""
메모리(DRAM/NAND) 현물가 브리핑 핸들러.

`/메모리` 명령 + 화~금 08:00 KST 자동 발송(월요일은 반도체 브리핑이 커버 → 충돌 회피).
결정론적 가격 카드 먼저 → Gemini 코멘트. 채널(BROADCAST_ID)로 발송.
"""
import logging
import asyncio

from clients import bot_client, _executor
from config import BROADCAST_ID
from services.memory_spot import (
    fetch_dram_spot, fetch_memory_proxies,
    format_spot_card, format_data_block, build_memory_prompt,
)
from services.gemini import generate_with_retry
from utils import kst_now

log = logging.getLogger(__name__)


async def send_memory_briefing():
    """화~금 08:00 KST 자동 실행 + /메모리 수동 호출."""
    loop = asyncio.get_running_loop()
    try:
        spot = await loop.run_in_executor(_executor, fetch_dram_spot)
    except Exception as e:
        log.error("메모리 현물가 수집 실패: %s", e)
        spot = []

    # 현물가가 비면 가시적으로 알림 — 조용한 no-op으로 매일 빈 카드 보내지 않음.
    if not spot:
        await bot_client.send_message(
            BROADCAST_ID,
            "⚠️ 메모리 현물가 조회 실패 — DRAMeXchange 접속 불가/페이지 구조 변경 가능. 점검 필요."
        )
        return

    try:
        proxies = await loop.run_in_executor(_executor, fetch_memory_proxies)
    except Exception as e:
        log.warning("메모리 프록시 수집 실패: %s", e)
        proxies = []

    # 1) 결정론적 가격 카드 먼저
    card = format_spot_card(spot, proxies, kst_now())
    try:
        await bot_client.send_message(BROADCAST_ID, card, parse_mode='md')
    except Exception as e:
        log.error("메모리 카드 전송 실패: %s", e)
        return

    # 2) Gemini 코멘트 (실패해도 위 카드는 이미 발송됨)
    today = kst_now().strftime('%Y년 %m월 %d일')
    block = format_data_block(spot, proxies)
    prompt = build_memory_prompt(block, today)
    try:
        resp = await loop.run_in_executor(_executor, generate_with_retry, [prompt])
        await bot_client.send_message(BROADCAST_ID, resp.text)
        log.info("메모리 현물가 브리핑 전송 완료")
    except Exception as e:
        log.warning("Gemini 메모리 코멘트 실패: %s", e)
        await bot_client.send_message(
            BROADCAST_ID, "⚠️ AI 코멘트 생성 오류 — 위 현물가 데이터만 참고하세요."
        )
