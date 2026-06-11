"""services/portfolio.py evaluate_portfolio — 가격 stub 주입으로 외부 API 없이 검증."""
import pytest
import services.portfolio as sp


@pytest.fixture
def stub_prices(monkeypatch):
    """get_current_price / get_usd_krw를 결정론적 stub으로 교체."""
    prices = {}
    monkeypatch.setattr(sp, 'get_current_price', lambda tk: prices.get(tk))
    monkeypatch.setattr(sp, 'get_usd_krw', lambda: 1000.0)  # 환산 검증 쉬운 값
    return prices


def test_us_position_converted_to_krw(stub_prices):
    stub_prices['NVDA'] = 150.0
    result = sp.evaluate_portfolio({
        'NVDA': {'name': '엔비디아', 'market': 'US', 'shares': 2, 'avg_price': 100.0},
    })
    row = result['rows'][0]
    assert row['pct'] == pytest.approx(50.0)
    assert row['value_local'] == pytest.approx(300.0)
    assert row['value_krw'] == pytest.approx(300_000.0)   # ×1000 환율 적용
    assert result['total_value_krw'] == pytest.approx(300_000.0)


def test_kr_position_no_fx(stub_prices):
    stub_prices['005930.KS'] = 1100.0
    result = sp.evaluate_portfolio({
        '005930.KS': {'name': '삼성전자', 'market': 'KR', 'shares': 10, 'avg_price': 1000.0},
    })
    row = result['rows'][0]
    assert row['value_krw'] == pytest.approx(11_000.0)    # 환율 미적용
    assert result['total_pct'] == pytest.approx(10.0)


def test_failed_price_isolated_to_row(stub_prices):
    # KX하이텍 조회 실패해도 나머지 종목 평가는 계속되어야 한다
    stub_prices['NVDA'] = 150.0
    stub_prices['052900.KQ'] = None
    result = sp.evaluate_portfolio({
        '052900.KQ': {'name': 'KX하이텍', 'market': 'KR', 'shares': 100, 'avg_price': 1500.0},
        'NVDA':      {'name': '엔비디아', 'market': 'US', 'shares': 1, 'avg_price': 100.0},
    })
    errors = [r for r in result['rows'] if r.get('error')]
    valid  = [r for r in result['rows'] if 'pct' in r]
    assert len(errors) == 1 and errors[0]['ticker'] == '052900.KQ'
    assert len(valid) == 1
    assert result['total_value_krw'] == pytest.approx(150_000.0)  # 실패 종목 제외 합산


def test_empty_portfolio_no_zerodivision(stub_prices):
    result = sp.evaluate_portfolio({})
    assert result['rows'] == []
    assert result['total_pct'] == 0.0


def test_zero_avg_price_no_zerodivision(stub_prices):
    # JSON 직접 편집 등으로 avg_price=0 레코드가 들어와도 죽지 않아야 한다
    stub_prices['NVDA'] = 150.0
    result = sp.evaluate_portfolio({
        'NVDA': {'name': '엔비디아', 'market': 'US', 'shares': 1, 'avg_price': 0.0},
    })
    assert result['rows'][0]['pct'] == 0.0
