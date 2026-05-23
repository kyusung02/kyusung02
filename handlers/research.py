"""
오늘의 증권사 리포트 핸들러 — /오늘리포트 + 평일 07:30 자동 발송.

보유·관심 종목과 겹치는 리포트를 별표 섹션으로 위에 띄우고, 나머지는 전체 섹션에 정렬.
"""
import logging
import asyncio
from datetime import date
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from services.dart_service import get_today_research_sync, _escape_md
from storage import load_watchlist, load_portfolio

log = logging.getLogger(__name__)

# 텔레그램 메시지 한도(4096) - 마크다운/링크 손상 방지를 위해 행 단위로 자른다.
_TG_SAFE_LIMIT = 4000


def _format_today_research(rows: list[dict]) -> list[str]:
    """리포트 행 리스트 → 텔레그램에 보낼 1~N 청크로 분할된 마크다운 텍스트."""
    today_str = date.today().strftime('%Y.%m.%d')
    if not rows:
        return [f"📄 **오늘의 증권사 리포트** ({today_str})\n(오늘 등록된 리포트가 없습니다)"]

    interesting = set(load_watchlist()) | set(load_portfolio().keys())
    starred, others = [], []
    for r in rows:
        line = f"**{_escape_md(r['stock'])}** | [{_escape_md(r['title'])}]({r['link']})"
        if r['broker']:
            line += f" — {_escape_md(r['broker'])}"
        if r['stock'] in interesting:
            starred.append("⭐ " + line)
        else:
            others.append("• " + line)

    parts = [f"📄 **오늘의 증권사 리포트** ({today_str}, {len(rows)}건)"]
    if starred:
        parts.append("")
        parts.append("─── 🌟 보유·관심 종목 ───")
        parts.extend(starred)
    if others:
        parts.append("")
        parts.append("─── 📋 전체 ───")
        parts.extend(others)

    # 행 단위 청크 — 한 메시지가 _TG_SAFE_LIMIT 넘으면 새 청크 시작.
    chunks, buf, size = [], [], 0
    for p in parts:
        plen = len(p) + 1
        if size + plen > _TG_SAFE_LIMIT and buf:
            chunks.append('\n'.join(buf))
            buf, size = [], 0
        buf.append(p)
        size += plen
    if buf:
        chunks.append('\n'.join(buf))
    return chunks


async def send_daily_research():
    """평일 07:30 자동 실행 + /오늘리포트 명령어로 수동 호출."""
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(_executor, get_today_research_sync)
        for chunk in _format_today_research(rows):
            await bot_client.send_message(MY_TELEGRAM_ID, chunk, parse_mode='md', link_preview=False)
        log.info("오늘의 리포트 전송 완료 (%d건)", len(rows))
    except Exception as e:
        log.error("오늘의 리포트 전송 실패: %s", e)
        await bot_client.send_message(MY_TELEGRAM_ID, "⚠️ 오늘의 리포트 생성 중 오류가 발생했습니다.")
