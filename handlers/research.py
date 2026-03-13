"""
증권사 리포트 자동 수집 핸들러
네이버 금융 리서치에서 당일 발행된 리포트를 수집하여 텔레그램으로 전송
매일 08:00 / 09:00 / 10:00 KST 자동 실행 (+ /리포트 명령어로 수동 호출)
"""
import logging
import asyncio
import re
import urllib.request
import urllib.parse
from datetime import datetime, date
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID

log = logging.getLogger(__name__)

# ── Naver Finance 리서치 URL ─────────────────────────────────────────────────
_COMPANY_URL  = "https://finance.naver.com/research/company_list.naver"   # 종목분석
_INDUSTRY_URL = "https://finance.naver.com/research/industry_list.naver"  # 산업분석
_NAVER_BASE   = "https://finance.naver.com"

# ── 요일 한국어 변환 ──────────────────────────────────────────────────────────
_WEEKDAY_KR = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']


# ── urllib 안전 오프너 (리다이렉트 허용, EUC-KR 한글 URL) ────────────────────
def _make_opener():
    opener = urllib.request.build_opener(
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPCookieProcessor(),
    )
    opener.addheaders = [
        ("User-Agent",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
         "AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/120.0.0.0 Safari/537.36"),
        ("Accept-Language", "ko-KR,ko;q=0.9"),
        ("Referer", "https://finance.naver.com/"),
    ]
    return opener


# ── 공통: HTML 가져오기 ───────────────────────────────────────────────────────
def _fetch_html(url: str, page: int = 1) -> str:
    """페이지 HTML을 EUC-KR로 디코딩하여 반환"""
    full_url = f"{url}?page={page}" if page > 1 else url
    opener = _make_opener()
    try:
        with opener.open(full_url, timeout=10) as resp:
            raw = resp.read()
        return raw.decode("euc-kr", errors="replace")
    except Exception as e:
        log.warning(f"HTML 가져오기 실패 ({full_url}): {e}")
        return ""


# ── 종목분석 리포트 크롤링 ────────────────────────────────────────────────────
def _parse_company_reports(html: str, today_str: str) -> list[dict]:
    """
    종목분석 HTML에서 당일 발행 리포트 추출
    반환: [{"title": str, "writer": str, "firm": str, "url": str, "date": str}]
    """
    reports = []
    # 테이블 행 패턴 — 네이버 리서치 company_list 구조
    row_pattern = re.compile(
        r'<tr[^>]*class="[^"]*"[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE,
    )
    # 날짜 셀 (마지막 td)
    date_pattern  = re.compile(r'(\d{2}\.\d{2}\.\d{2})')
    # 제목 + 링크
    title_pattern = re.compile(
        r'<a[^>]+href="(/research/company_read\.naver\?[^"]+)"[^>]*>'
        r'(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    # 증권사 이름 (두 번째 링크 없는 td)
    firm_pattern  = re.compile(
        r'<td[^>]*class="[^"]*tc[^"]*"[^>]*>\s*([가-힣A-Za-z0-9& ]+?)\s*</td>',
        re.DOTALL | re.IGNORECASE,
    )

    for row_m in row_pattern.finditer(html):
        row_html = row_m.group(1)
        # 날짜 확인
        date_m = date_pattern.search(row_html)
        if not date_m:
            continue
        row_date = date_m.group(1)          # 예: "26.03.13"
        # today_str 형식: "26.03.13"
        if row_date != today_str:
            continue
        # 제목 + URL
        title_m = title_pattern.search(row_html)
        if not title_m:
            continue
        rel_url   = title_m.group(1)
        raw_title = title_m.group(2)
        title     = re.sub(r'<[^>]+>', '', raw_title).strip()
        full_url  = _NAVER_BASE + rel_url
        # 증권사
        firm_m = firm_pattern.search(row_html)
        firm   = firm_m.group(1).strip() if firm_m else "미상"
        if title:
            reports.append({
                "title": title,
                "firm":  firm,
                "url":   full_url,
                "date":  row_date,
            })
    return reports


# ── 산업분석 리포트 크롤링 ────────────────────────────────────────────────────
def _parse_industry_reports(html: str, today_str: str) -> list[dict]:
    """
    산업분석 HTML에서 당일 발행 리포트 추출
    company_list와 구조 유사하나 URL path가 industry_read
    """
    reports = []
    row_pattern = re.compile(
        r'<tr[^>]*>(.*?)</tr>',
        re.DOTALL | re.IGNORECASE,
    )
    date_pattern  = re.compile(r'(\d{2}\.\d{2}\.\d{2})')
    title_pattern = re.compile(
        r'<a[^>]+href="(/research/industry_read\.naver\?[^"]+)"[^>]*>'
        r'(.*?)</a>',
        re.DOTALL | re.IGNORECASE,
    )
    firm_pattern  = re.compile(
        r'<td[^>]*class="[^"]*tc[^"]*"[^>]*>\s*([가-힣A-Za-z0-9& ]+?)\s*</td>',
        re.DOTALL | re.IGNORECASE,
    )

    for row_m in row_pattern.finditer(html):
        row_html = row_m.group(1)
        date_m = date_pattern.search(row_html)
        if not date_m:
            continue
        if date_m.group(1) != today_str:
            continue
        title_m = title_pattern.search(row_html)
        if not title_m:
            continue
        rel_url   = title_m.group(1)
        raw_title = title_m.group(2)
        title     = re.sub(r'<[^>]+>', '', raw_title).strip()
        full_url  = _NAVER_BASE + rel_url
        firm_m = firm_pattern.search(row_html)
        firm   = firm_m.group(1).strip() if firm_m else "미상"
        if title:
            reports.append({
                "title": title,
                "firm":  firm,
                "url":   full_url,
                "date":  today_str,
            })
    return reports


# ── 데이터 수집 (동기 — executor에서 실행) ───────────────────────────────────
def _fetch_all_reports() -> dict:
    """
    네이버 금융 리서치에서 당일 리포트 수집
    반환: {"company": [...], "industry": [...]}
    """
    today     = date.today()
    today_str = today.strftime("%y.%m.%d")   # 예: "26.03.13"

    company_reports  = []
    industry_reports = []

    # 종목분석 — 최대 3페이지 수집 (당일 리포트가 끊길 때까지)
    for page in range(1, 4):
        html = _fetch_html(_COMPANY_URL, page)
        if not html:
            break
        parsed = _parse_company_reports(html, today_str)
        if not parsed and page > 1:
            break                          # 당일 리포트 소진
        company_reports.extend(parsed)
        if len(parsed) < 20:
            break                          # 마지막 페이지

    # 산업분석 — 최대 2페이지
    for page in range(1, 3):
        html = _fetch_html(_INDUSTRY_URL, page)
        if not html:
            break
        parsed = _parse_industry_reports(html, today_str)
        if not parsed and page > 1:
            break
        industry_reports.extend(parsed)
        if len(parsed) < 20:
            break

    # 중복 제거 (제목 기준)
    seen: set[str] = set()

    def dedup(lst):
        result = []
        for item in lst:
            if item["title"] not in seen:
                seen.add(item["title"])
                result.append(item)
        return result

    return {
        "company":  dedup(company_reports),
        "industry": dedup(industry_reports),
        "date":     today_str,
    }


# ── 메시지 포매팅 ─────────────────────────────────────────────────────────────
_MAX_COMPANY  = 30   # 종목분석 최대 표시 건수
_MAX_INDUSTRY = 10   # 산업분석 최대 표시 건수


def _format_message(data: dict) -> list[str]:
    """
    수집된 리포트를 텔레그램 메시지로 포매팅.
    텔레그램 4096자 제한 때문에 메시지를 여러 개로 분할하여 반환.
    """
    now      = datetime.now()
    weekday  = _WEEKDAY_KR[now.weekday()]
    date_str = now.strftime(f"%Y.%m.%d {weekday}")
    hour_str = now.strftime("%H:%M")

    company_list  = data.get("company",  [])[:_MAX_COMPANY]
    industry_list = data.get("industry", [])[:_MAX_INDUSTRY]
    total = len(company_list) + len(industry_list)

    messages = []

    # ── 헤더 메시지 ──────────────────────────────────────────────────────────
    header_lines = [
        f"📋 **오늘의 증권사 리포트**",
        f"📅 {date_str}  {hour_str}",
        f"📌 종목분석 {len(company_list)}건 · 산업분석 {len(industry_list)}건",
        "",
    ]
    messages.append("\n".join(header_lines))

    # ── 종목분석 리포트 ───────────────────────────────────────────────────────
    if company_list:
        buf = ["─── 📊 종목분석 ───\n"]
        for i, r in enumerate(company_list, 1):
            line = f"{i}. [{r['firm']}] [{r['title']}]({r['url']})"
            buf.append(line)
            # 4000자 넘기 전에 메시지 분할
            if sum(len(l) for l in buf) > 3800:
                messages.append("\n".join(buf))
                buf = []   # 다음 묶음 시작
        if buf:
            messages.append("\n".join(buf))
    else:
        messages.append("─── 📊 종목분석 ───\n⚠️ 오늘 발행된 종목분석 리포트가 없습니다.")

    # ── 산업분석 리포트 ───────────────────────────────────────────────────────
    if industry_list:
        buf = ["─── 🏭 산업분석 ───\n"]
        for i, r in enumerate(industry_list, 1):
            line = f"{i}. [{r['firm']}] [{r['title']}]({r['url']})"
            buf.append(line)
        if buf:
            messages.append("\n".join(buf))
    else:
        messages.append("─── 🏭 산업분석 ───\n⚠️ 오늘 발행된 산업분석 리포트가 없습니다.")

    # ── 푸터 ─────────────────────────────────────────────────────────────────
    messages.append(f"✅ 총 {total}건 수집 완료")

    return messages


# ── 외부에서 호출되는 메인 함수 ───────────────────────────────────────────────
async def send_research_briefing():
    """
    매일 08:00 / 09:00 / 10:00 KST 자동 실행.
    /리포트 명령어로 수동 호출도 가능.
    """
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(_executor, _fetch_all_reports)
        if not data.get("company") and not data.get("industry"):
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                "⚠️ 오늘 발행된 증권사 리포트를 찾지 못했습니다.\n"
                "(장 시작 전이거나 주말/공휴일일 수 있습니다.)"
            )
            return
        messages = _format_message(data)
        for msg in messages:
            if msg.strip():
                await bot_client.send_message(
                    MY_TELEGRAM_ID, msg, parse_mode="md", link_preview=False
                )
                await asyncio.sleep(0.3)   # 텔레그램 flood 방지
        log.info(f"리포트 브리핑 전송 완료 — "
                 f"종목분석 {len(data.get('company',[]))}건, "
                 f"산업분석 {len(data.get('industry',[]))}건")
    except Exception as e:
        log.error(f"리포트 브리핑 전송 실패: {e}")
        await bot_client.send_message(
            MY_TELEGRAM_ID, f"⚠️ 리포트 브리핑 오류: {e}"
        )
