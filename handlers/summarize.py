"""
요약 핸들러 — PDF 업로드 / URL / YouTube 자동 요약
사용 방법:
  - PDF 파일 전송  →  자동 감지 후 리포트 요약
  - /요약 https://... →  웹 기사/페이지 요약
  - YouTube URL 포함 메시지 전송  →  자막 기반 요약
"""
import logging
import asyncio
import os
import re
import tempfile
from telethon import events
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
from utils import fetch_webpage_text, extract_youtube_id
from services.gemini import client as gemini_client, PDF_PROMPT, LINK_PROMPT

log = logging.getLogger(__name__)

# ── YouTube 자막 라이브러리 ────────────────────────────────────────────────────
try:
    from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound
    _YOUTUBE_OK = True
except ImportError:
    _YOUTUBE_OK = False
    log.warning("youtube_transcript_api 미설치 — YouTube 요약 비활성화")

# ── URL 패턴 ──────────────────────────────────────────────────────────────────
_URL_RE = re.compile(r'https?://[^\s]+')

# Gemini 모델
_GEMINI_MODEL = "gemini-2.5-flash"


# ════════════════════════════════════════════════════════════════════════════
# 내부 함수 (동기 — executor에서 실행)
# ════════════════════════════════════════════════════════════════════════════

def _summarize_pdf_sync(file_path: str) -> str:
    """Gemini Files API로 PDF 업로드 후 요약"""
    uploaded = gemini_client.files.upload(file=file_path)
    response = gemini_client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[PDF_PROMPT, uploaded],
    )
    return response.text


def _summarize_url_sync(url: str) -> str:
    """웹페이지 텍스트 추출 후 Gemini 요약"""
    text = fetch_webpage_text(url)
    if not text or len(text.strip()) < 100:
        return "⚠️ 페이지 내용을 가져오지 못했습니다. (접근 제한 또는 빈 페이지)"
    prompt = f"{LINK_PROMPT}\n\n[원문]\n{text[:5000]}"
    response = gemini_client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[prompt],
    )
    return response.text


def _summarize_youtube_sync(video_id: str) -> str:
    """YouTube 자막 추출 후 Gemini 요약"""
    if not _YOUTUBE_OK:
        return "⚠️ YouTube 요약 기능을 사용할 수 없습니다. (라이브러리 미설치)"
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(
            video_id, languages=["ko", "en"]
        )
        transcript = " ".join(t["text"] for t in transcript_list)[:5000]
    except Exception as e:
        return f"⚠️ 자막을 가져올 수 없습니다: {e}"
    prompt = f"{LINK_PROMPT}\n\n[YouTube 자막]\n{transcript}"
    response = gemini_client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=[prompt],
    )
    return response.text


# ════════════════════════════════════════════════════════════════════════════
# Telegram 핸들러
# ════════════════════════════════════════════════════════════════════════════

def register(bot):
    """
    main.py에서 register(bot_client) 한 번만 호출하면
    이 파일의 모든 핸들러가 등록됩니다.
    """

    # ── ① PDF 파일 업로드 자동 감지 ─────────────────────────────────────────
    @bot.on(events.NewMessage(from_users=MY_TELEGRAM_ID, func=lambda e: e.document))
    async def handle_pdf(event):
        """사용자가 PDF 파일을 전송하면 자동으로 요약"""
        doc = event.document
        if not doc:
            return

        # MIME 타입 또는 파일명으로 PDF 확인
        mime = getattr(doc, "mime_type", "") or ""
        fname = ""
        for attr in getattr(doc, "attributes", []):
            if hasattr(attr, "file_name"):
                fname = attr.file_name or ""
                break

        is_pdf = (mime == "application/pdf") or fname.lower().endswith(".pdf")
        if not is_pdf:
            return  # PDF가 아니면 무시 (이미지, 동영상 등)

        display_name = fname or "업로드된 파일"
        await event.respond(f"📄 **{display_name}** 분석 중...\n(Gemini AI 처리 중, 약 20~40초 소요)")

        # 임시 파일에 저장
        suffix = ".pdf"
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp_path = tmp.name
            await bot.download_media(event.message, file=tmp_path)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                _executor, _summarize_pdf_sync, tmp_path
            )
            header = f"📋 **{display_name} 요약**\n\n"
            await event.respond(header + result, parse_mode="md", link_preview=False)
        except Exception as e:
            log.error(f"PDF 요약 실패: {e}")
            await event.respond(f"⚠️ PDF 요약 중 오류 발생: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    # ── ② /요약 명령어 (URL 또는 YouTube) ──────────────────────────────────
    @bot.on(events.NewMessage(from_users=MY_TELEGRAM_ID, pattern=r'^/요약\s*(.*)$'))
    async def handle_summary_cmd(event):
        """
        /요약 https://...   → 웹 기사 또는 YouTube 요약
        /요약               → 앞서 보낸 URL 메시지에 대한 안내
        """
        raw = (event.pattern_match.group(1) or "").strip()

        # URL 없이 /요약 만 입력한 경우
        if not raw:
            await event.respond(
                "📎 사용법:\n"
                "• `/요약 https://기사URL` — 뉴스·리포트 요약\n"
                "• `/요약 https://youtu.be/xxxxx` — YouTube 요약\n"
                "• PDF 파일 전송 — 자동 요약",
                parse_mode="md",
            )
            return

        # URL 추출
        url_match = _URL_RE.search(raw)
        if not url_match:
            await event.respond("⚠️ URL을 인식하지 못했습니다. `https://` 로 시작하는 링크를 포함해 주세요.")
            return

        url = url_match.group(0)

        # YouTube 여부 판단
        yt_id = extract_youtube_id(url)
        loop = asyncio.get_running_loop()

        if yt_id:
            await event.respond(f"🎬 YouTube 요약 중... (자막 추출 → AI 분석, 약 20초)")
            try:
                result = await loop.run_in_executor(
                    _executor, _summarize_youtube_sync, yt_id
                )
                await event.respond(f"🎬 **YouTube 요약**\n\n{result}", parse_mode="md", link_preview=False)
            except Exception as e:
                log.error(f"YouTube 요약 실패: {e}")
                await event.respond(f"⚠️ YouTube 요약 실패: {e}")
        else:
            await event.respond(f"🔍 페이지 분석 중...\n{url}")
            try:
                result = await loop.run_in_executor(
                    _executor, _summarize_url_sync, url
                )
                await event.respond(f"📰 **링크 요약**\n\n{result}", parse_mode="md", link_preview=False)
            except Exception as e:
                log.error(f"URL 요약 실패: {e}")
                await event.respond(f"⚠️ 링크 요약 실패: {e}")
