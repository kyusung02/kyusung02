"""
채널 모니터링 핸들러 — 채널 메시지 자동 요약 (링크/유튜브/PDF)
"""
import os
import re
import time
import uuid
import logging
import asyncio
from telethon import events
from clients import bot_client, user_client, _executor
from config import MY_TELEGRAM_ID, DOWNLOAD_DIR
from services.gemini import client, LINK_PROMPT, PDF_PROMPT
from storage import load_channels, save_channels
from utils import extract_youtube_id, fetch_webpage_text
from youtube_transcript_api import YouTubeTranscriptApi

log = logging.getLogger(__name__)


def _gemini_generate(fn, max_retries=4, base_delay=5):
    """503/429 오류 시 지수 백오프 재시도."""
    for attempt in range(max_retries):
        try:
            return fn()
        except Exception as e:
            msg = str(e)
            if attempt < max_retries - 1 and ('503' in msg or '429' in msg or 'UNAVAILABLE' in msg or 'quota' in msg.lower()):
                delay = base_delay * (2 ** attempt)
                log.warning(f"Gemini 일시 오류({e}), {delay}초 후 재시도 ({attempt+1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


async def update_channel_handler(channels: list):
    """채널 목록 변경 시 user_client 이벤트 핸들러를 재등록합니다."""
    user_client.remove_event_handler(on_channel_msg)
    if channels:
        user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=channels))
        log.info("채널 핸들러 재등록: %s", channels)
    else:
        log.info("모니터링 채널 없음 — 핸들러 비활성화")


async def on_channel_msg(event):
    """
    WATCH_CHANNELS 에 등록된 채널에 새 메시지가 올라올 때 자동 실행됩니다.

    [유튜브] YouTube URL 감지 → 자막 추출 → Gemini 요약 → 원문 링크 첨부
    [기사/블로그] 일반 URL 감지 → 웹 크롤링 → Gemini 요약 → 원문 링크 첨부
    [PDF] 문서 첨부 감지 → 파일 다운로드 → Gemini Files API 업로드 → 리포트 분석 → 파일 삭제
    """
    chat = await event.get_chat()
    source_name = chat.title if hasattr(chat, 'title') else "정보 채널"
    log.info("채널 메시지 수신: %s | 내용 앞 50자: %s", source_name, (event.text or "")[:50])

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
                transcript = ' '.join(s['text'] for s in segments)[:5000]
                _tr = transcript
                res = await loop.run_in_executor(
                    _executor,
                    lambda: _gemini_generate(lambda: client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[LINK_PROMPT + f"\n내용: {_tr}"]
                    ))
                )
                await bot_client.send_message(MY_TELEGRAM_ID, f"{source_header}\n\n{res.text}")
            except Exception as e:
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"{source_header}\n\n⚠️ 자막을 가져올 수 없습니다: {e}"
                )
        return

    # ── 기사 / 블로그 ────────────────────────────────────────────────────────
    if urls:
        source_header = f"🌐 **[네모 봇 인사이트 요약]**\n📡 출처 채널: {source_name}"
        if caption_text:
            source_header += f"\n📌 원문: {caption_text[:200]}"
        source_header += f"\n🔗 링크: {urls[0]}"
        text = await loop.run_in_executor(_executor, fetch_webpage_text, urls[0])
        if text:
            _t = text
            res = await loop.run_in_executor(
                _executor,
                lambda: _gemini_generate(lambda: client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[LINK_PROMPT + f"\n내용: {_t}"]
                ))
            )
            await bot_client.send_message(MY_TELEGRAM_ID, f"{source_header}\n\n{res.text}")
        return

    # ── PDF (문서 첨부) ───────────────────────────────────────────────────────
    if event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        file_path = await event.download_media(file=DOWNLOAD_DIR)
        caption   = (event.text or '').strip() or '(제목 없음)'
        filename  = os.path.basename(file_path) if file_path else '다운로드 실패'

        if not file_path:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신 실패]**\n{caption}\n\n⚠️ 파일 다운로드에 실패했습니다."
            )
            return

        try:
            file_path.encode('ascii')
        except UnicodeEncodeError:
            safe_path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.pdf")
            os.rename(file_path, safe_path)
            file_path = safe_path

        pdf_header = f"📡 출처 채널: {source_name}\n📌 원문: {caption}\n📎 파일명: {filename}"
        await bot_client.send_message(
            MY_TELEGRAM_ID,
            f"📄 **[PDF 분석 중...]**\n{pdf_header}\n\n⏳ Gemini가 리포트를 읽고 있습니다..."
        )
        uploaded = None
        try:
            uploaded = await loop.run_in_executor(
                _executor, lambda: client.files.upload(file=file_path)
            )
            _up = uploaded
            response = await loop.run_in_executor(
                _executor,
                lambda: _gemini_generate(lambda: client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[_up, PDF_PROMPT]
                ))
            )
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📊 **[PDF 리포트 분석]**\n{pdf_header}\n\n{response.text}"
            )
        except Exception as e:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신]**\n{pdf_header}\n\n⚠️ 분석 중 오류 발생: {e}"
            )
        finally:
            if uploaded:
                try:
                    await loop.run_in_executor(
                        _executor, lambda: client.files.delete(name=uploaded.name)
                    )
                except Exception:
                    pass
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception:
                    pass
