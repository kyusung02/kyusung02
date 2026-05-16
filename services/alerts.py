"""
가격·이벤트 알림 체크 — yfinance 종가 vs 임계치 매칭 + 트리거 1회성 제거.

설계:
- 같은 ticker 여러 알림은 1회만 가격 조회 (그루핑).
- 트리거된 알림은 즉시 alerts.json 에서 제거 (스팸 방지).
- 가격 조회 실패는 무시하고 다음 ticker 진행.
"""
import logging
import yfinance as yf
from storage import load_alerts, save_alerts

log = logging.getLogger(__name__)


def _get_price_pair(ticker: str) -> tuple[float | None, float | None]:
    """현재가(=마지막 종가) + 전일 종가. 두 값 모두 없으면 (None, None)."""
    try:
        hist = yf.Ticker(ticker).history(period='3d')
        if hist.empty:
            return None, None
        cur  = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2]) if len(hist) >= 2 else None
        return cur, prev
    except Exception as e:
        log.warning("alert price 조회 실패 %s: %s", ticker, e)
        return None, None


def _is_triggered(alert: dict, cur: float, prev: float | None) -> bool:
    t = alert.get('type')
    v = alert.get('value')
    if v is None:
        return False
    if t == 'above':
        return cur >= v
    if t == 'below':
        return cur <= v
    if prev is None:
        return False
    chg_pct = (cur / prev - 1) * 100
    if t == 'pct_up':
        return chg_pct >= v
    if t == 'pct_down':
        return chg_pct <= -v
    return False


def check_alerts() -> list[dict]:
    """활성 알림 전체 검사. 트리거된 항목만 반환(파일에서도 제거).

    반환 항목에는 triggered_price·prev_close 가 함께 들어가 메시지 포맷에 사용.
    """
    alerts = load_alerts()
    if not alerts:
        return []

    by_ticker: dict[str, list] = {}
    for a in alerts:
        by_ticker.setdefault(a['ticker'], []).append(a)

    triggered: list[dict] = []
    triggered_ids: set[str] = set()

    for ticker, group in by_ticker.items():
        cur, prev = _get_price_pair(ticker)
        if cur is None:
            continue
        for a in group:
            if _is_triggered(a, cur, prev):
                a['triggered_price'] = cur
                a['prev_close']      = prev
                triggered.append(a)
                triggered_ids.add(a['id'])

    if triggered_ids:
        survivors = [a for a in alerts if a.get('id') not in triggered_ids]
        save_alerts(survivors)
    return triggered
