"""
증권사 리포트 자동 수집 핸들러
네이버 금융 리서치에서 당일 발행된 리포트를 수집하여 텔레그램으로 전송
매일 08:00 / 09:00 / 10:00 KST 자동 실행 (+ /리포트 명령어로 수동 호출)
"""
import logging
import asyncio
import re
import urllib.request
from datetime import datetime, date
from clients import bot_client, _executor
from config import MY_TELEGRAM_ID
log = logging.getLogger(__name__)
_COMPANY_URL  = "https://finance.naver.com/research/company_list.naver"
_INDUSTRY_URL = "https://finance.naver.com/research/industry_list.naver"
_NAVER_BASE   = "https://finance.naver.com/research"
_WEEKDAY_KR = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']
_MAX_COMPANY  = 30
_MAX_INDUSTRY = 10
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
def _fetch_html(url: str, page: int = 1) -> str:
    full_url = f"{url}?page={page}" if page > 1 else url
    try:
        with _make_opener().open(full_url, timeout=10) as resp:
            return resp.read().decode("euc-kr", errors="replace")
    except Exception as e:
        log.warning(f"HTML 가져오기 실패 ({full_url}): {e}")
        return ""
def _parse_reports(html: str, today_str: str, read_path: str) -> list[dict]:
    """
    네이버 리서치 종목분석·산업분석 공통 파서.
    실제 HTML 구조 (2025년 기준):
    <tr>
      <td>종목명(또는 섹터)</td>
      <td><a href="company_read.naver?nid=...">제목</a></td>
      <td>증권사</td>
      <td class="file"><a href="https://...pdf">...</a></td>
      <td>26.03.23</td>
      <td>조회수</td>
    </tr>
    """
    reports = []
    # 모든 <tr> 블록 추출
    row_re    = re.compile(r'<tr>(.*?)</tr>', re.DOTALL)
    td_re     = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
    title_re  = re.compile(
        r'href="(' + re.escape(read_path) + r'[^"]+)"[^>]*>\s*(.*?)\s*(?:<img[^>]*>)?\s*</a>',
        re.DOTALL | re.IGNORECASE,
    )
    pdf_re    = re.compile(
        r'href="(https://stock\.pstatic\.net/[^"]+\.pdf)"',
        re.IGNORECASE,
    )
    for row_m in row_re.finditer(html):
        row_html = row_m.group(1)
        tds = td_re.findall(row_html)
        if len(tds) < 5:
            continue
        # td[4] = 날짜
        row_date = re.sub(r'<[^>]+>', '', tds[4]).strip()
        if row_date != today_str:
            continue
        # 제목 + 상세 URL
        title_m = title_re.search(tds[1])
        if not title_m:
            continue
        rel_url = title_m.group(1)
        title   = re.sub(r'<[^>]+>', '', title_m.group(2)).strip()
        if not title:
            continue
        detail_url = f"{_NAVER_BASE}/{rel_url}"
        # PDF 링크 (td[3])
        pdf_m   = pdf_re.search(tds[3])
        pdf_url = pdf_m.group(1) if pdf_m else ""
        # 증권사 (td[2])
        firm = re.sub(r'<[^>]+>', '', tds[2]).strip()
        # 종목명/섹터 (td[0])
        subject = re.sub(r'<[^>]+>', '', tds[0]).strip()
        reports.append({
            "title":      title,
            "firm":       firm or "미상",
            "subject":    subject,
            "url":        detail_url,
            "pdf_url":    pdf_url,
            "date":       row_date,
        })
    # 중복 제거 (제목 기준)
    seen, result = set(), []
    for r in reports:
        if r["title"] not in seen:
            seen.add(r["title"])
            result.append(r)
    return result
def _fetch_all_reports() -> dict:
    today_str = date.today().strftime("%y.%m.%d")
    company_reports, industry_reports = [], []
    # 종목분석 최대 3페이지
    for page in range(1, 4):
        html   = _fetch_html(_COMPANY_URL, page)
        parsed = _parse_reports(html, today_str, "company_read.naver")
        company_reports.extend(parsed)
        if not html or len(parsed) < 20:
            break
    # 산업분석 최대 2페이지
    for page in range(1, 3):
        html   = _fetch_html(_INDUSTRY_URL, page)
        parsed = _parse_reports(html, today_str, "industry_read.naver")
        industry_reports.extend(parsed)
        if not html or len(parsed) < 20:
            break
    return {
        "company":  company_reports[:_MAX_COMPANY],
        "industry": industry_reports[:_MAX_INDUSTRY],
        "date":     today_str,
    }
def _format_message(data: dict) -> list[str]:
    now      = datetime.now()
    weekday  = _WEEKDAY_KR[now.weekday()]
    date_str = now.strftime(f"%Y.%m.%d {weekday}")
    hour_str = now.strftime("%H:%M")
    company_list  = data.get("company",  [])
    industry_list = data.get("industry", [])
    total = len(company_list) + len(industry_list)
    messages = []
    # 헤더
    messages.append(
        f"📋 **오늘의 증권사 리포트**\n"
        f"📅 {date_str}  {hour_str}\n"
        f"📌 종목분석 {len(company_list)}건 · 산업분석 {len(industry_list)}건"
    )
    # 종목분석
    if company_list:
        buf = ["─── 📊 종목분석 ───\n"]
        for i, r in enumerate(company_list, 1):
            pdf_tag = f" · [PDF]({r['pdf_url']})" if r.get("pdf_url") else ""
            line    = f"{i}. **{r['subject']}** [{r['title']}]({r['url']}) — {r['firm']}{pdf_tag}"
            buf.append(line)
            if sum(len(l) for l in buf) > 3600:
                messages.append("\n".join(buf))
                buf = []
        if buf:
            messages.append("\n".join(buf))
    else:
        messages.append("─── 📊 종목분석 ───\n⚠️ 오늘 발행된 종목분석 리포트가 없습니다.")
    # 산업분석
    if industry_list:
        buf = ["─── 🏭 산업분석 ───\n"]
        for i, r in enumerate(industry_list, 1):
            pdf_tag = f" · [PDF]({r['pdf_url']})" if r.get("pdf_url") else ""
            line    = f"{i}. **{r['subject']}** [{r['title']}]({r['url']}) — {r['firm']}{pdf_tag}"
            buf.append(line)
        messages.append("\n".join(buf))
    else:
        messages.append("─── 🏭 산업분석 ───\n⚠️ 오늘 발행된 산업분석 리포트가 없습니다.")
    messages.append(f"✅ 총 {total}건 수집 완료")
    return messages
async def send_research_briefing():
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
        for msg in _format_message(data):
            if msg.strip():
                await bot_client.send_message(
                    MY_TELEGRAM_ID, msg, parse_mode="md", link_preview=False
                )
                await asyncio.sleep(0.3)
        log.info(
            f"리포트 브리핑 전송 완료 — "
            f"종목분석 {len(data.get('company', []))}건, "
            f"산업분석 {len(data.get('industry', []))}건"
        )
    except Exception as e:
        log.error(f"리포트 브리핑 전송 실패: {e}")
        await bot_client.send_message(MY_TELEGRAM_ID, f"⚠️ 리포트 브리핑 오류: {e}")
