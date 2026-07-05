"""매크로 일정(services.macro) 검증 — 정적 테이블이라 네트워크 없이 전부 결정론."""
from datetime import datetime

from utils import KST
from services.macro import (
    _EVENTS,
    _FULL_COVERAGE_END,
    _parsed_events,
    upcoming_macro_events,
    format_macro_lines,
)


def _kst(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def test_table_parses_and_sorted():
    # 오타(존재하지 않는 날짜 등)는 파싱에서 즉시 터지고, 정렬 위반도 여기서 잡는다
    parsed = _parsed_events()
    assert len(parsed) == len(_EVENTS)
    dts = [dt for dt, _ in parsed]
    assert dts == sorted(dts)


def test_window_filtering():
    now = _kst("2026-07-10 07:00")
    events = upcoming_macro_events(days=7, now=now)
    # 7일 내엔 7/14 CPI 하나뿐 (다음 이벤트 7/30 FOMC는 창 밖)
    assert len(events) == 1
    assert "6월 CPI" in events[0][1]


def test_today_tomorrow_markers():
    text = format_macro_lines(days=7, now=_kst("2026-07-14 07:00"))
    assert "오늘 21:30 미국 6월 CPI" in text

    text = format_macro_lines(days=7, now=_kst("2026-07-13 07:00"))
    assert "내일 21:30 미국 6월 CPI" in text


def test_regular_date_format_with_weekday():
    # 7/28 시점: 7/30(목) 새벽 FOMC 결과가 창 안
    text = format_macro_lines(days=7, now=_kst("2026-07-28 07:00"))
    assert "7/30(목) 03:00 FOMC 금리결정 발표" in text


def test_empty_window_returns_none():
    # 다음 이벤트(7/30)까지 14일 남은 시점 + 커버리지 경고 발동 전 → None
    assert format_macro_lines(days=7, now=_kst("2026-07-16 07:00")) is None


def test_coverage_warning_near_exhaustion():
    # 전체 커버 종료(2026-12-10) 30일 이내로 들어오면 보이는 경고를 붙인다
    text = format_macro_lines(days=7, now=_kst("2026-12-01 07:00"))
    assert "갱신 필요" in text

    # 아직 여유 있을 땐 경고 없음
    text = format_macro_lines(days=7, now=_kst("2026-07-14 07:00"))
    assert "갱신 필요" not in text


def test_coverage_end_matches_table():
    # _FULL_COVERAGE_END는 CPI/고용 마지막 이벤트 날짜와 일치해야 한다 (갱신 누락 방지)
    bls_dates = [dt.date() for dt, label in _parsed_events()
                 if "CPI" in label or "고용보고서" in label]
    assert max(bls_dates) == _FULL_COVERAGE_END
