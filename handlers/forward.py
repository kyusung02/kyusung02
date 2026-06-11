"""
채널 포워딩 핸들러 — 지정한 구독 채널의 새 메시지를 내 채널로 원문 그대로 전달.

요약(channel.py)과 독립 동작 — CHANNEL_SUMMARY_ENABLED 와 무관하게
포워딩 소스 + 대상이 설정되어 있으면 활성화된다. 같은 채널이 모니터링/포워딩
양쪽에 등록되어 있으면 두 핸들러가 각각 동작한다(의도된 동작).

전제: user_client(본인 계정)가 소스 채널에 가입되어 있고,
대상 채널에 본인 계정 글쓰기 권한이 있어야 한다(본인 소유 채널이면 기본).
"""
import logging
from telethon import events
from telethon.errors import ChatForwardsRestrictedError
from clients import user_client
from config import DOWNLOAD_DIR
from storage import (
    load_forward_config, set_forward_target, clear_forward_target,
    add_forward_source, remove_forward_source, normalize_channel,
)
from utils import cleanup_files

log = logging.getLogger(__name__)


async def update_forward_handler():
    """포워딩 설정 변경 시 user_client 이벤트 핸들러 재등록."""
    cfg = load_forward_config()
    user_client.remove_event_handler(on_forward_msg)
    if cfg["sources"] and cfg["target"] is not None:
        user_client.add_event_handler(on_forward_msg, events.NewMessage(chats=cfg["sources"]))
        log.info("포워딩 핸들러 재등록 (%d개 채널 → %s)", len(cfg["sources"]), cfg["target"])
    else:
        log.info("포워딩 소스/대상 미설정 — 핸들러 비활성화")


async def on_forward_msg(event):
    """소스 채널 새 메시지 → 대상 채널로 원문 포워드.

    원채널이 콘텐츠 보호(포워딩 금지)면 복사 전송으로 폴백:
    미디어는 다운로드 후 재업로드, 텍스트는 출처 헤더를 붙여 게시.
    """
    cfg = load_forward_config()
    target = cfg.get("target")
    if target is None:
        return
    try:
        await user_client.forward_messages(target, event.message)
        return
    except ChatForwardsRestrictedError:
        log.info("포워딩 금지 채널 — 복사 전송 폴백 (chat=%s)", event.chat_id)
    except Exception as e:
        log.warning("포워드 실패 (chat=%s → %s): %s", event.chat_id, target, e)
        return

    # ── 복사 전송 폴백 (포워딩 금지 채널) ───────────────────────────────────
    try:
        chat = await event.get_chat()
        source_name = getattr(chat, 'title', None) or str(event.chat_id)
        header = f"📨 출처: {source_name}\n\n"
        text = event.text or ''
        if event.message.media:
            path = await event.download_media(file=DOWNLOAD_DIR)
            if path:
                try:
                    # 캡션 한도 1024자
                    await user_client.send_file(target, path, caption=(header + text)[:1024])
                finally:
                    cleanup_files([path])
                return
        if text:
            await user_client.send_message(target, (header + text)[:4096], link_preview=False)
    except Exception as e:
        log.warning("복사 전송 폴백 실패 (chat=%s): %s", event.chat_id, e)


# ── 명령어 핸들러 (/포워딩대상 /포워딩추가 /포워딩삭제 /포워딩목록) ───────────

async def handle_forward_target(event, args_text: str):
    ch = args_text.strip()
    if not ch:
        cfg = load_forward_config()
        cur = cfg.get('target')
        await event.reply(
            "사용법: /포워딩대상 @채널유저네임 (또는 비공개 채널 숫자 ID)\n"
            "해제: /포워딩대상 해제\n"
            f"현재 대상: {cur if cur is not None else '(미설정)'}"
        )
        return
    if ch in ('해제', '삭제', 'off'):
        clear_forward_target()
        await update_forward_handler()
        await event.reply("🗑️ 포워딩 대상이 해제됐습니다. (포워딩 비활성 — 소스 목록은 유지)")
        return
    cfg = set_forward_target(ch)
    await update_forward_handler()
    await event.reply(
        f"✅ 포워딩 대상 채널: {cfg['target']}\n\n"
        "⚠️ 본인 계정이 해당 채널에 글쓰기 권한이 있어야 합니다."
    )


async def handle_forward_add(event, args_text: str):
    ch = args_text.strip()
    if not ch:
        await event.reply("사용법: /포워딩추가 @채널유저네임 (또는 비공개 채널 숫자 ID)")
        return
    ok, cfg = add_forward_source(ch)
    if not ok:
        await event.reply(f"'{normalize_channel(ch)}'은(는) 이미 포워딩 중입니다.")
        return
    await update_forward_handler()
    msg = (
        f"✅ '{normalize_channel(ch)}' 포워딩 소스에 추가됐습니다. (총 {len(cfg['sources'])}개)\n\n"
        "⚠️ user_client 계정이 해당 채널에 가입되어 있어야 합니다."
    )
    if cfg.get('target') is None:
        msg += "\n❗ 포워딩 대상이 미설정입니다 — `/포워딩대상 @내채널` 로 설정하세요."
    await event.reply(msg)


async def handle_forward_remove(event, args_text: str):
    ch = args_text.strip()
    if not ch:
        await event.reply("사용법: /포워딩삭제 @채널유저네임 (또는 비공개 채널 숫자 ID)")
        return
    ok, cfg = remove_forward_source(ch)
    if not ok:
        await event.reply(f"'{normalize_channel(ch)}'은(는) 포워딩 목록에 없습니다.")
        return
    await update_forward_handler()
    await event.reply(f"🗑️ '{normalize_channel(ch)}' 포워딩 소스에서 삭제됐습니다. (총 {len(cfg['sources'])}개)")


async def handle_forward_list(event):
    cfg = load_forward_config()
    target = cfg.get('target')
    sources = cfg.get('sources') or []
    lines = ["📨 **채널 포워딩 설정**", ""]
    lines.append(f"대상: {target if target is not None else '(미설정 — /포워딩대상 @내채널)'}")
    if sources:
        lines.append(f"소스 ({len(sources)}개):")
        for i, ch in enumerate(sources, 1):
            lines.append(f"{i}. {ch}")
    else:
        lines.append("소스: (없음 — /포워딩추가 @채널)")
    active = bool(sources) and target is not None
    lines += ["", f"상태: {'🟢 활성' if active else '⚪ 비활성 (소스·대상 모두 설정 필요)'}"]
    await event.reply("\n".join(lines))
