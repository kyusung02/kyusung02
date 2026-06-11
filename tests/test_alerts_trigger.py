"""services/alerts.py _is_triggered — 9종 알림 타입 판정 로직 (순수 함수)."""
import pytest
from services.alerts import _is_triggered


def _ind(**kw):
    """지표 dict 기본값 — 필요한 키만 덮어쓴다."""
    base = {
        'cur': None, 'prev': None, 'high_52w': None, 'low_52w': None,
        'cur_volume': None, 'avg_volume_20': None, 'rsi_14': None,
    }
    base.update(kw)
    return base


# ── above / below — 경계값 ──────────────────────────────────────────────────

def test_above_boundary():
    assert _is_triggered({'type': 'above', 'value': 100.0}, _ind(cur=100.0)) is True
    assert _is_triggered({'type': 'above', 'value': 100.0}, _ind(cur=99.99)) is False


def test_below_boundary():
    assert _is_triggered({'type': 'below', 'value': 100.0}, _ind(cur=100.0)) is True
    assert _is_triggered({'type': 'below', 'value': 100.0}, _ind(cur=100.01)) is False


def test_value_none_returns_false():
    assert _is_triggered({'type': 'above', 'value': None}, _ind(cur=100.0)) is False
    assert _is_triggered({'type': 'below'}, _ind(cur=100.0)) is False


# ── pct_up / pct_down ───────────────────────────────────────────────────────

def test_pct_up():
    # +10% 정확히 도달 → 트리거
    assert _is_triggered({'type': 'pct_up', 'value': 10.0}, _ind(cur=110.0, prev=100.0)) is True
    assert _is_triggered({'type': 'pct_up', 'value': 10.1}, _ind(cur=110.0, prev=100.0)) is False


def test_pct_up_prev_none():
    assert _is_triggered({'type': 'pct_up', 'value': 5.0}, _ind(cur=110.0, prev=None)) is False


def test_pct_down_positive_value_contract():
    # 계약: value는 양수(파싱 경로가 보장) — "5% 하락" = value 5.0
    assert _is_triggered({'type': 'pct_down', 'value': 5.0}, _ind(cur=95.0, prev=100.0)) is True
    assert _is_triggered({'type': 'pct_down', 'value': 5.1}, _ind(cur=95.0, prev=100.0)) is False


def test_pct_down_negative_value_inverts():
    # 회귀 감시: 음수 value가 들어오면 부등호가 반전된다(파싱 경로가 양수를 보장해야 하는 이유).
    # 자연어 파싱(parse_alert_intent)이 음수를 내보내면 이 동작으로 오발화한다.
    assert _is_triggered({'type': 'pct_down', 'value': -5.0}, _ind(cur=99.0, prev=100.0)) is True


# ── 52주 신고가/신저가 ──────────────────────────────────────────────────────

def test_52w_high_equal_triggers():
    assert _is_triggered({'type': '52w_high'}, _ind(cur=500.0, high_52w=500.0)) is True
    assert _is_triggered({'type': '52w_high'}, _ind(cur=499.0, high_52w=500.0)) is False


def test_52w_low_equal_triggers():
    assert _is_triggered({'type': '52w_low'}, _ind(cur=100.0, low_52w=100.0)) is True
    assert _is_triggered({'type': '52w_low'}, _ind(cur=101.0, low_52w=100.0)) is False


def test_52w_missing_data():
    assert _is_triggered({'type': '52w_high'}, _ind(cur=500.0, high_52w=None)) is False


# ── volume_spike ────────────────────────────────────────────────────────────

def test_volume_spike():
    ind = _ind(cur=10.0, cur_volume=300.0, avg_volume_20=100.0)
    assert _is_triggered({'type': 'volume_spike', 'value': 3.0}, ind) is True
    ind2 = _ind(cur=10.0, cur_volume=299.0, avg_volume_20=100.0)
    assert _is_triggered({'type': 'volume_spike', 'value': 3.0}, ind2) is False


def test_volume_spike_zero_avg_guard():
    # 평균 거래량 0 → ZeroDivision 없이 False
    ind = _ind(cur=10.0, cur_volume=300.0, avg_volume_20=0.0)
    assert _is_triggered({'type': 'volume_spike', 'value': 3.0}, ind) is False


# ── RSI ─────────────────────────────────────────────────────────────────────

def test_rsi_above():
    assert _is_triggered({'type': 'rsi_above', 'value': 70.0}, _ind(cur=10.0, rsi_14=75.0)) is True
    assert _is_triggered({'type': 'rsi_above', 'value': 70.0}, _ind(cur=10.0, rsi_14=70.0)) is False


def test_rsi_below():
    assert _is_triggered({'type': 'rsi_below', 'value': 30.0}, _ind(cur=10.0, rsi_14=25.0)) is True
    assert _is_triggered({'type': 'rsi_below', 'value': 30.0}, _ind(cur=10.0, rsi_14=30.0)) is False


def test_rsi_none_returns_false():
    assert _is_triggered({'type': 'rsi_above', 'value': 70.0}, _ind(cur=10.0, rsi_14=None)) is False


# ── 공통 가드 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('t', [
    'above', 'below', 'pct_up', 'pct_down', '52w_high', '52w_low',
    'volume_spike', 'rsi_above', 'rsi_below',
])
def test_cur_none_always_false(t):
    ind = _ind(cur=None, prev=100.0, high_52w=1.0, low_52w=1.0,
               cur_volume=1.0, avg_volume_20=1.0, rsi_14=50.0)
    assert _is_triggered({'type': t, 'value': 1.0}, ind) is False


def test_unknown_type_false():
    assert _is_triggered({'type': 'no_such_type', 'value': 1.0}, _ind(cur=100.0)) is False
