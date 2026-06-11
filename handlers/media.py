"""
미디어 분석 핸들러 — 봇 채팅 직접 입력: PDF 첨부 분석, URL(유튜브/웹) 요약.

main.py 에서 분리(2026-06). 요약 로직은 services.gemini.summarize_text 로 일원화
(채널 모니터링 handlers/channel.py 와 동일 경로 — 프롬프트 변경 시 한 곳만 수정).
"""
import os
import re
import logging
import asyncio
from clients import _executor
from config import DOWNLOAD_DIR
from services.gemini import analyze_pdf, summarize_text
from utils import extract_youtube_id, fetch_webpage_text
from youtube_transcript_api import YouTubeTranscriptApi

log = logging.getLogger(__name__)


async def handle_pdf(event, text: str):
    """봇 채팅 PDF 첨부 → 분석. analyze_pdf가 경로 검증 + 정리 책임."""
    file_path = await event.download_media(file=DOWNLOAD_DIR)
    caption   = (text or '').strip() or '(제목 없음)'

    if not file_path:
        await event.reply("⚠️ PDF 다운로드에 실패했습니다.")
        return

    filename = os.path.basename(file_path)
    await event.reply("📄 **PDF 분석 중...**\n⏳ Gemini가 리포트를 읽고 있습니다...")
    try:
        answer = await analyze_pdf(file_path, _executor)
        await event.reply(
            f"📊 **[PDF 리포트 분석]**\n_{caption}_\n\n{answer}\n\n📎 파일명: {filename}"
        )
    except Exception as e:
        log.warning("PDF 분석 실패 (%s): %s", file_path, e)
        await event.reply("⚠️ PDF 분석 중 오류가 발생했습니다.")


async def handle_url(event, text: str):
    """봇 채팅 URL → YouTube 자막 또는 웹 본문 요약."""
    urls = re.findall(r'https?://[^\s]+', text)
    if not urls:
        return
    url  = urls[0]
    loop = asyncio.get_running_loop()

    if re.search(r'(youtube\.com/watch|youtu\.be/)', url):
        vid_id = extract_youtube_id(url)
        if not vid_id:
            await event.reply("⚠️ YouTube 영상 ID를 인식할 수 없습니다.")
            return
        await event.reply("▶️ **유튜브 자막 추출 중...**")
        try:
            segments = await loop.run_in_executor(
                _executor,
                lambda: YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
            )
            transcript = ' '.join(s['text'] for s in segments)
            summary = await loop.run_in_executor(_executor, summarize_text, transcript)
            await event.reply(f"▶️ **[유튜브 인사이트 요약]**\n\n{summary}\n\n📎 원문: {url}")
        except Exception as e:
            log.warning("YouTube 자막/요약 실패 (%s): %s", url, e)
            await event.reply(f"⚠️ 자막을 가져올 수 없습니다.\n📎 원문: {url}")
        return

    await event.reply("🌐 **링크 분석 중...**")
    page_text = await loop.run_in_executor(_executor, fetch_webpage_text, url)
    if not page_text:
        await event.reply(f"⚠️ 해당 링크에서 내용을 가져오지 못했습니다.\n📎 {url}")
        return
    try:
        summary = await loop.run_in_executor(_executor, summarize_text, page_text)
        await event.reply(f"🌐 **[네모 봇 인사이트 요약]**\n\n{summary}\n\n📎 원문: {url}")
    except Exception as e:
        log.warning("URL 요약 실패 (%s): %s", url, e)
        await event.reply("⚠️ 요약 중 오류가 발생했습니다.")
