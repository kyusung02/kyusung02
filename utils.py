"""
공통 유틸리티 — 웹 크롤링, YouTube ID 추출
"""
import re
import urllib.request
import urllib.parse


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """리다이렉트 URL에 한글 등 non-ASCII가 있을 때 percent-encode 후 추적"""
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
        return super().redirect_request(req, fp, code, msg, headers, newurl)


safe_opener = urllib.request.build_opener(_SafeRedirectHandler())


def extract_youtube_id(url: str) -> str | None:
    match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', url)
    return match.group(1) if match else None


def fetch_webpage_text(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()[:5000]
    except Exception:
        return None
