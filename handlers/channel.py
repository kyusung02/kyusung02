"""
채널 모니터링 핸들러 — 채널 메시지 자동 요약 (링크/유튜브/PDF).
"""
import re
import logging
import asyncio
from telethon import events
from clients import bot_client, user_client, _executor
from config import MY_TELEGRAM_ID, DOWNLOAD_DIR, CHANNEL_SUMMARY_ENABLED
from services.gemini import (
    LINK_PROMPT, generate_with_retry, _wrap_external, analyze_pdf,
)
from utils import extract_youtube_id, fetch_webpage_text
from youtube_transcript_api import YouTubeTranscriptApi

log = logging.getLogger(__name__)


async def update_channel_handler(channels: list):
    """채널 목록 변경 시 user_client 이벤트 핸들러를 재등록합니다.

    CHANNEL_SUMMARY_ENABLED=false 인 경우 channels.json 저장만 반영하고
    실제 핸들러 재등록은 스킵합니다(요약 서비스 비활성 보장).
    """
    if not CHANNEL_SUMMARY_ENABLED:
        log.info("채널 요약 서비스 비활성 — 핸들러 재등록 스킵 (channels.json 만 갱신됨)")
        return
    user_client.remove_event_handler(on_channel_msg)
    if channels:
        user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=channels))
        log.info("채널 핸들러 재등록 (%d개 채널)", len(channels))
    else:
        log.info("모니터링 채널 없음 — 핸들러 비활성화")


def _summarize_external(prompt_body: str) -> str:
    """LINK_PROMPT + 외부 콘텐츠 구획 → Gemini 호출 (동기)."""
    contents = [LINK_PROMPT + "\n\n" + _wrap_external(prompt_body)]
    response = generate_with_retry(contents)
    return response.text


async def on_channel_msg(event):
    """
    WATCH_CHANNELS 에 등록된 채널에 새 메시지가 올라올 때 자동 실행됩니다.

    [유튜브] YouTube URL 감지 → 자막 추출 → Gemini 요약 → 원문 링크 첨부
    [기사/블로그] 일반 URL 감지 → 웹 크롤링 → Gemini 요약 → 원문 링크 첨부
    [PDF] 문서 첨부 감지 → 파일 다운로드 → Gemini Files API 업로드 → 리포트 분석 → 파일 삭제
    """
    chat = await event.get_chat()
    source_name = chat.title if hasattr(chat, 'title') else "정보 채널"
    # 메시지 본문은 민감정보 가능성 — INFO에 길이만 기록, 본문은 DEBUG 레벨
    log.info("채널 메시지 수신: %s | 길이=%d", source_name, len(event.text or ""))
    log.debug("채널 메시지 본문 앞 50자: %s", (event.text or "")[:50])

    urls = re.findall(r'https?://[^\s]+', event.text or "")
    original_text = (event.text or '').strip()
    caption_text  = re.sub(r'https?://[^\s]+', '', original_text).strip()
    loop = asyncio.get_running_loop()

    # ── 유튜브 ──────────────────────────────────────────────────────────────
    if urls and re.search(r'(youtube\.com/watch|youtu\.be/)', urls[0]):
        yt_url = urls[0]
        vid_id = extract_youtube_id(yt_url)
        if vid_id:
            source_header = f"▶️ **[유튜브 인사이트 요약]**\n📡 출처 채널: {source_name}"
            if caption_text:
                source_header += f"\n📌 원문: {caption_text[:200]}"
            source_header += f"\n🔗 링크: {yt_url}"
            try:
                segments = await loop.run_in_executor(
                    _executor,
                    lambda: YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
                )
                transcript = ' '.join(s['text'] for s in segments)
                summary = await loop.run_in_executor(_executor, _summarize_external, transcript)
                await bot_client.send_message(MY_TELEGRAM_ID, f"{source_header}\n\n{summary}")
            except Exception as e:
                log.warning("YouTube 자막/요약 실패 (%s): %s", yt_url, e)
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"{source_header}\n\n⚠️ 자막을 가져올 수 없습니다."
                )
        return

    # ── 기사 / 블로그 ────────────────────────────────────────────────────────
    if urls:
        source_header = f"🌐 **[네모 봇 인사이트 요약]**\n📡 출처 채널: {source_name}"
        if caption_text:
            source_header += f"\n📌 원문: {caption_text[:200]}"
        source_header += f"\n🔗 링크: {urls[0]}"
        text = await loop.run_in_executor(_executor, fetch_webpage_text, urls[0])
        if not text:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"{source_header}\n\n⚠️ 해당 링크에서 내용을 가져오지 못했습니다."
            )
            return
        try:
            summary = await loop.run_in_executor(_executor, _summarize_external, text)
            await bot_client.send_message(MY_TELEGRAM_ID, f"{source_header}\n\n{summary}")
        except Exception as e:
            log.warning("URL 요약 실패 (%s): %s", urls[0], e)
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"{source_header}\n\n⚠️ 요약 중 오류가 발생했습니다."
            )
        return

    # ── PDF (문서 첨부) ───────────────────────────────────────────────────────
    if event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        file_path = await event.download_media(file=DOWNLOAD_DIR)
        caption   = (event.text or '').strip() or '(제목 없음)'

        if not file_path:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신 실패]**\n{caption}\n\n⚠️ 파일 다운로드에 실패했습니다."
            )
            return

        pdf_header = f"📡 출처 채널: {source_name}\n📌 원문: {caption}"
        await bot_client.send_message(
            MY_TELEGRAM_ID,
            f"📄 **[PDF 분석 중...]**\n{pdf_header}\n\n⏳ Gemini가 리포트를 읽고 있습니다..."
        )
        try:
            # analyze_pdf는 path traversal 검증 + 업로드/분석/정리를 한 번에 처리
            answer = await analyze_pdf(file_path, _executor)
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📊 **[PDF 리포트 분석]**\n{pdf_header}\n\n{answer}"
            )
        except Exception as e:
            log.warning("PDF 분석 실패 (%s): %s", file_path, e)
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신]**\n{pdf_header}\n\n⚠️ 분석 중 오류가 발생했습니다."
            )
