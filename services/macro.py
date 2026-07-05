"""
매크로 일정 — FOMC·CPI·고용보고서 발표 일정 (정적 테이블, 네트워크 0)

경제 캘린더 스크레이핑(investing.com 등)은 봇 차단·마크업 변경에 취약해서 쓰지 않는다.
대신 연준·BLS가 1년 치를 사전 공표하는 공식 일정을 검증해 코드에 내장한다(결정론).
모닝 시황 브리핑의 data_block에 붙어 Gemini '리스크 & 기회' 섹션의 사실 근거가 되고,
원본 데이터 블록에도 그대로 노출된다.

출처 (2026-07-05 확인):
- FOMC: federalreserve.gov/monetarypolicy/fomccalendars.htm (2026·2027 전체)
- CPI/고용보고서: bls.gov/schedule/news_release/{cpi,empsit}.htm (2026년분까지 공표)

시각은 전부 KST로 변환해 저장한다. 미국 서머타임(2026: 3/8~11/1, 2027: 3/14~11/7)
경계를 넘는 항목은 겨울 시간(+1시간)으로 이미 보정돼 있음:
- BLS 발표 08:30 ET → 서머타임 21:30 / 겨울 22:30 KST 당일
- FOMC 성명 14:00 ET(회의 둘째 날) → 서머타임 익일 03:00 / 겨울 익일 04:00 KST
"""
import logging
from datetime import date, datetime, timedelta

from utils import KST, kst_now

log = logging.getLogger(__name__)

# (KST 발표시각 'YYYY-MM-DD HH:MM', 표기명) — 시간순 정렬 유지
_EVENTS = [
    # ── 2026 하반기 ──────────────────────────────────────────────
    ("2026-07-14 21:30", "미국 6월 CPI"),
    ("2026-07-30 03:00", "FOMC 금리결정 발표 (7/28~29 회의)"),
    ("2026-08-07 21:30", "미국 7월 고용보고서"),
    ("2026-08-12 21:30", "미국 7월 CPI"),
    ("2026-09-04 21:30", "미국 8월 고용보고서"),
    ("2026-09-11 21:30", "미국 8월 CPI"),
    ("2026-09-17 03:00", "FOMC 금리결정·점도표 발표 (9/15~16 회의)"),
    ("2026-10-02 21:30", "미국 9월 고용보고서"),
    ("2026-10-14 21:30", "미국 9월 CPI"),
    ("2026-10-29 03:00", "FOMC 금리결정 발표 (10/27~28 회의)"),
    ("2026-11-06 22:30", "미국 10월 고용보고서"),
    ("2026-11-10 22:30", "미국 10월 CPI"),
    ("2026-12-04 22:30", "미국 11월 고용보고서"),
    ("2026-12-10 04:00", "FOMC 금리결정·점도표 발표 (12/8~9 회의)"),
    ("2026-12-10 22:30", "미국 11월 CPI"),
    # ── 2027 (FOMC만 — BLS는 2027년분 미공표) ────────────────────
    ("2027-01-28 04:00", "FOMC 금리결정 발표 (1/26~27 회의)"),
    ("2027-03-18 03:00", "FOMC 금리결정·점도표 발표 (3/16~17 회의)"),
    ("2027-04-29 03:00", "FOMC 금리결정 발표 (4/27~28 회의)"),
    ("2027-06-10 03:00", "FOMC 금리결정·점도표 발표 (6/8~9 회의)"),
    ("2027-07-29 03:00", "FOMC 금리결정 발표 (7/27~28 회의)"),
    ("2027-09-16 03:00", "FOMC 금리결정·점도표 발표 (9/14~15 회의)"),
    ("2027-10-28 03:00", "FOMC 금리결정 발표 (10/26~27 회의)"),
    ("2027-12-09 04:00", "FOMC 금리결정·점도표 발표 (12/7~8 회의)"),
]

# 세 시리즈가 '모두' 커버되는 마지막 날짜 = BLS 2026년분 끝(11월 CPI, 12/10 발표).
# FOMC만 2027까지 있어서 '테이블 마지막 이벤트'로는 CPI/고용 소진을 못 잡는다.
# 테이블 갱신 시(BLS 2027년분 공표는 통상 가을) 이 값도 같이 올릴 것.
_FULL_COVERAGE_END = date(2026, 12, 10)

_WEEKDAY_KR = ('월', '화', '수', '목', '금', '토', '일')


def _parsed_events():
    """테이블을 KST aware datetime으로 파싱 (오타는 여기서 즉시 ValueError)."""
    return [(datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=KST), label)
            for ts, label in _EVENTS]


def upcoming_macro_events(days: int = 7, now: datetime | None = None):
    """[now, now+days] 창 안의 이벤트 목록. 시간순 정렬."""
    now = now or kst_now()
    end = now + timedelta(days=days)
    return [(dt, label) for dt, label in _parsed_events() if now <= dt <= end]


def format_macro_lines(days: int = 7, now: datetime | None = None) -> str | None:
    """모닝 브리핑 data_block용 매크로 일정 텍스트. 창 안에 이벤트 없으면 None.

    날짜·시각은 이 함수가 결정론적으로 만들고 Gemini는 코멘트만 단다(수치 환각 차단
    원칙과 동일 결). 커버리지 소진이 임박하면 조용히 죽는 대신 보이는 경고를 붙인다.
    """
    now = now or kst_now()
    events = upcoming_macro_events(days=days, now=now)

    lines = []
    today = now.date()
    for dt, label in events:
        d = dt.date()
        if d == today:
            day_str = "오늘"
        elif d == today + timedelta(days=1):
            day_str = "내일"
        else:
            day_str = f"{d.month}/{d.day}({_WEEKDAY_KR[d.weekday()]})"
        lines.append(f"- {day_str} {dt:%H:%M} {label}")

    # CPI·고용보고서 커버리지 소진 임박 경고(30일 전부터) — 갱신을 잊으면
    # '일정 없음'이 '조용한 누락'으로 둔갑하므로 브리핑에 보이게 남긴다.
    if today > _FULL_COVERAGE_END - timedelta(days=30):
        lines.append("- ⚠️ CPI·고용보고서 일정 테이블 소진 임박 — BLS 신규 일정 갱신 필요")
        log.warning("매크로 일정 테이블 커버리지 소진 임박 (전체 커버 %s까지)", _FULL_COVERAGE_END)

    return '\n'.join(lines) if lines else None
