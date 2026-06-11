"""storage.py — 매매 기록(가중평균/매도)과 채널 정규화. conftest가 DATA_DIR을 임시폴더로 주입."""
import os
import pytest
import storage


@pytest.fixture(autouse=True)
def clean_portfolio():
    """각 테스트 전 포트폴리오 파일 초기화."""
    if os.path.exists(storage.PORTFOLIO_PATH):
        os.remove(storage.PORTFOLIO_PATH)
    yield


# ── add_buy ─────────────────────────────────────────────────────────────────

def test_add_buy_new_position():
    ok, _ = storage.add_buy('005930.KS', '삼성전자', 'KR', 10, 80000, '2026-06-11')
    assert ok
    p = storage.load_portfolio()
    assert p['005930.KS']['shares'] == 10
    assert p['005930.KS']['avg_price'] == 80000
    assert p['005930.KS']['market'] == 'KR'


def test_add_buy_weighted_average():
    storage.add_buy('NVDA', '엔비디아', 'US', 10, 100.0, '2026-06-01')
    ok, _ = storage.add_buy('NVDA', '엔비디아', 'US', 10, 200.0, '2026-06-11')
    assert ok
    p = storage.load_portfolio()
    assert p['NVDA']['shares'] == 20
    assert p['NVDA']['avg_price'] == pytest.approx(150.0)  # (10*100 + 10*200) / 20


def test_add_buy_rejects_nonpositive():
    ok1, _ = storage.add_buy('NVDA', '엔비디아', 'US', 0, 100.0, '2026-06-11')
    ok2, _ = storage.add_buy('NVDA', '엔비디아', 'US', 10, -1.0, '2026-06-11')
    assert ok1 is False and ok2 is False
    assert storage.load_portfolio() == {}


# ── add_sell ────────────────────────────────────────────────────────────────

def test_add_sell_partial_keeps_avg():
    storage.add_buy('NVDA', '엔비디아', 'US', 10, 150.0, '2026-06-01')
    ok, _ = storage.add_sell('NVDA', 4)
    assert ok
    p = storage.load_portfolio()
    assert p['NVDA']['shares'] == 6
    assert p['NVDA']['avg_price'] == pytest.approx(150.0)  # 부분 매도는 평단 유지


def test_add_sell_full_removes_position():
    storage.add_buy('NVDA', '엔비디아', 'US', 10, 150.0, '2026-06-01')
    ok, _ = storage.add_sell('NVDA')  # shares=None → 전량
    assert ok
    assert 'NVDA' not in storage.load_portfolio()


def test_add_sell_over_held_treated_as_full():
    storage.add_buy('NVDA', '엔비디아', 'US', 10, 150.0, '2026-06-01')
    ok, _ = storage.add_sell('NVDA', 999)
    assert ok
    assert 'NVDA' not in storage.load_portfolio()


def test_add_sell_unknown_ticker():
    ok, msg = storage.add_sell('TSLA', 1)
    assert ok is False
    assert '보유' in msg


# ── normalize_channel ───────────────────────────────────────────────────────

def test_normalize_channel_numeric_id_stays_int():
    assert storage.normalize_channel('-1001234567890') == -1001234567890
    assert storage.normalize_channel(-100) == -100


def test_normalize_channel_username_gets_at():
    assert storage.normalize_channel('somechannel') == '@somechannel'
    assert storage.normalize_channel('@somechannel') == '@somechannel'
