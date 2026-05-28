"""
반도체 업황 스크리닝 핸들러.

`/semi` 또는 `/반도체` 명령 + 매주 월요일 08:00 KST 자동 발송.
주말 누적 흐름 + 한 주 시작 시점에 매크로/메모리/장비 종합 그림 제공.
"""
import logging
import asyncio

from clients import bot_client, _executor
from config import BROADCAST_ID
from services.semi import (
    fetch_semi_data, format_semi_data_block, build_semi_briefing_prompt,
)
from services.gemini import generate_with_retry
from utils import kst_now

log = logging.getLogger(__name__)

_WEEKDAY_KR = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']


def _format_header_message(data: dict) -> str:
    """Gemini 호출 전에 미리 보내는 정량 요약 (원본 데이터 카드)."""
    now = kst_now()
    weekday = _WEEKDAY_KR[now.weekday()]
    lines = [
        "🔬 **반도체 업황 스크리닝**",
        f"📅 {now.strftime('%Y.%m.%d')} {weekday}  {now.strftime('%H:%M')}",
        "",
        "─── 📊 지수·ETF ───",
    ]
    for name, info in data.get("indices", {}).items():
        arrow = "▲" if info["chg_pct"] >= 0 else "▼"
        sign  = "+" if info["chg_pct"] >= 0 else ""
        chg_5d = info.get("chg_5d")
        chg_20d = info.get("chg_20d")
        suffix = ""
        if chg_5d is not None and chg_20d is not None:
            suffix = f"  (5D {chg_5d:+.1f}%, 20D {chg_20d:+.1f}%)"
        lines.append(f"{arrow} {name}: {info['price']:,.2f}  {sign}{info['chg_pct']:.2f}%{suffix}")

    for group, items in data.get("groups", {}).items():
        lines += ["", f"─── 📈 {group} ───"]
        for it in items:
            arr = "▲" if it["chg_pct"] >= 0 else "▼"
            sign = "+" if it["chg_pct"] >= 0 else ""
            chg_5d = it.get("chg_5d")
            extra = f"  (5D {chg_5d:+.1f}%)" if chg_5d is not None else ""
            lines.append(
                f"{arr} {it['name']:<14}  {sign}{it['chg_pct']:.2f}%   "
                f"{it['price']:,.2f}{extra}"
            )

    if data.get("top_gainers"):
        lines += ["", "─── 🏆 상승 TOP 5 ───"]
        for i, (name, group, chg) in enumerate(data["top_gainers"], 1):
            lines.append(f"{i}. {name}  ▲{chg:.1f}%  [{group}]")

    if data.get("top_losers"):
        lines += ["", "─── 💥 하락 TOP 5 ───"]
        for i, (name, group, chg) in enumerate(data["top_losers"], 1):
            lines.append(f"{i}. {name}  ▼{abs(chg):.1f}%  [{group}]")

    return "\n".join(lines)


async def send_semi_briefing():
    """매주 월요일 08:00 KST 자동 실행 + /semi 수동 호출 가능."""
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(_executor, fetch_semi_data)
    except Exception as e:
        log.error("반도체 데이터 수집 실패: %s", e)
        await bot_client.send_message(BROADCAST_ID, "⚠️ 반도체 데이터 조회에 실패했습니다.")
        return

    if not data or not data.get("groups"):
        await bot_client.send_message(BROADCAST_ID, "⚠️ 반도체 데이터가 비어 있습니다.")
        return

    # 1) 정량 데이터 카드 먼저
    header_msg = _format_header_message(data)
    try:
        await bot_client.send_message(BROADCAST_ID, header_msg, parse_mode='md')
    except Exception as e:
        log.error("반도체 헤더 메시지 전송 실패: %s", e)
        return

    # 2) Gemini 코멘트 (PER vs PBR 프레임)
    today = kst_now().strftime('%Y년 %m월 %d일')
    data_block = format_semi_data_block(data)
    prompt = build_semi_briefing_prompt(data_block, today)

    try:
        response = await loop.run_in_executor(_executor, generate_with_retry, [prompt])
        await bot_client.send_message(BROADCAST_ID, response.text)
        log.info("반도체 브리핑 전송 완료")
    except Exception as e:
        log.warning("Gemini 반도체 코멘트 실패: %s", e)
        # 헤더는 이미 보냈으니 코멘트만 실패 알림
        await bot_client.send_message(
            BROADCAST_ID,
            "⚠️ AI 코멘트 생성 중 오류 — 위의 원본 데이터만 참고하세요."
        )
