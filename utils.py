"""
공통 유틸리티 — 웹 크롤링, YouTube ID 추출, 텔레그램 메시지 청크 분할, KST 시간
"""
import os
import re
import ipaddress
import socket
import logging
import urllib.request
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def kst_now() -> datetime:
    return datetime.now(KST)


def kst_today() -> date:
    return datetime.now(KST).date()


def channel_summary_enabled() -> bool:
    """채널 요약 서비스 활성화 여부 — env에서 직접 판독.

    config.py에서 import하지 않고 os.environ을 직접 읽어, VM의 config.py가
    구버전이라 변수가 없어도 ImportError로 죽지 않게 한다(2026-05-15 사고 패턴 회피).
    활성화: 환경변수 CHANNEL_SUMMARY_ENABLED=true.
    """
    return os.environ.get("CHANNEL_SUMMARY_ENABLED", "false").strip().lower() == "true"


log = logging.getLogger(__name__)

MAX_TG_MESSAGE_LEN = 4096

_PRIVATE_NETS = [
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('0.0.0.0/8'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fe80::/10'),
]


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """리다이렉트 URL에 한글 등 non-ASCII가 있을 때 percent-encode 후 추적.

    추가로 리다이렉트 대상 URL도 SSRF 검사를 통과해야 한다 — 그렇지 않으면 차단.
    """
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            newurl.encode('ascii')
        except UnicodeEncodeError:
            parsed = urllib.parse.urlsplit(newurl)
            newurl = urllib.parse.urlunsplit((
                parsed.scheme,
                parsed.netloc,
                urllib.parse.quote(parsed.path, safe='/'),
                urllib.parse.quote(parsed.query, safe='=&+%'),
                parsed.fragment,
            ))
        if not is_url_safe(newurl):
            raise urllib.error.HTTPError(newurl, 403, "Redirect to disallowed host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


safe_opener = urllib.request.build_opener(_SafeRedirectHandler())


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        return True
    return any(ip in net for net in _PRIVATE_NETS)


def is_url_safe(url: str) -> bool:
    """SSRF 차단: scheme/host 검증 + DNS 해석한 모든 IP가 공개 대역인지 확인."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if _is_private_ip(ip):
            return False
    return True


_YT_HOSTS = {'youtu.be', 'www.youtu.be', 'youtube.com', 'www.youtube.com', 'm.youtube.com'}
_YT_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')


def extract_youtube_id(url: str) -> str | None:
    """YouTube 호스트인지 확인 후 11자리 영상 ID 추출. 비-YouTube URL은 None."""
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or '').lower()
    if host not in _YT_HOSTS:
        return None
    if host.endswith('youtu.be'):
        candidate = (parsed.path or '/').lstrip('/').split('/', 1)[0]
        return candidate if _YT_ID.match(candidate) else None
    qs = urllib.parse.parse_qs(parsed.query or '')
    vid = (qs.get('v') or [None])[0]
    if vid and _YT_ID.match(vid):
        return vid
    m = re.match(r'/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})', parsed.path or '')
    return m.group(1) if m else None


def fetch_webpage_text(url: str) -> str | None:
    """공개 URL의 본문 텍스트만 추출 — SSRF 가드 통과 후에만 fetch."""
    if not is_url_safe(url):
        log.warning("fetch_webpage_text: SSRF 가드로 차단된 URL — %s", url)
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with safe_opener.open(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()[:5000]
    except Exception as e:
        log.debug("fetch_webpage_text 실패 (%s): %s", url, e)
        return None


async def reply_chunked(event, text: str, chunk: int = MAX_TG_MESSAGE_LEN) -> None:
    """Telegram 4096자 한도 초과 메시지를 줄 경계에서 분할 전송.

    글자 단위로 자르면 마크다운 엔티티(**굵게**·[링크]()·``` 코드블록)가 경계에서
    깨지므로 줄 단위로 누적한다. ``` 코드펜스가 청크 경계를 넘으면 현재 청크를 닫고
    다음 청크에서 다시 연다. 한 줄이 단독으로 한도를 넘으면 그 줄만 글자 단위 분할.
    """
    if not text:
        return

    _FENCE = '\n```'             # 펜스 열린 청크에 덧붙는 닫기(이어가기 시 여유분 예약)
    buf: list[str] = []
    size = 0
    fence_open = False

    async def _flush():
        nonlocal buf, size
        if not buf:
            return
        body = '\n'.join(buf)
        if fence_open:
            body += _FENCE           # 청크 안에서 열린 코드펜스를 닫아 엔티티 보존
        await event.reply(body)
        buf, size = (['```'], 4) if fence_open else ([], 0)  # 다음 청크에서 펜스 이어가기

    # 펜스가 버퍼 중간에서 열릴 수 있으므로 닫기 '\n```'(4자) 여유분을 항상 예약한다.
    limit = chunk - len(_FENCE)
    for line in text.split('\n'):
        if len(line) + 1 > limit:    # 단독으로 한도를 넘는 라인 — 최후수단 글자 분할
            await _flush()
            for i in range(0, len(line), chunk):
                await event.reply(line[i:i + chunk])
            continue
        if size + len(line) + 1 > limit:
            await _flush()
        buf.append(line)
        size += len(line) + 1
        if line.lstrip().startswith('```'):
            fence_open = not fence_open

    if buf:
        await event.reply('\n'.join(buf) + (_FENCE if fence_open else ''))
