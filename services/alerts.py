"""
가격·이벤트 알림 체크 — yfinance 1년치 종가/거래량으로 9종 타입 매칭.

타입:
- above / below             : 가격 임계
- pct_up / pct_down         : 전일 종가 대비 변동
- 52w_high / 52w_low        : 52주 신고가/신저가 (1년 = 약 252 거래일)
- volume_spike              : 20일 평균 거래량 × 배율
- rsi_above / rsi_below     : RSI(14) 임계 (Wilder smoothing)

설계:
- 같은 ticker 여러 알림은 1회만 yfinance 호출(그루핑).
- period='1y' 한 번 호출로 모든 지표 추출 — 호출 수 최소화.
- 트리거된 알림은 즉시 alerts.json 에서 제거(1회성).
- 1년치가 부족(신규 상장 등)하면 가능 범위 내에서 동작.
"""
import logging
import yfinance as yf
from storage import load_alerts, save_alerts

log = logging.getLogger(__name__)


def _rsi(closes, period: int = 14) -> float | None:
    """Wilder RSI. 데이터 부족 시 None."""
    if closes is None or len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain  = delta.clip(lower=0)
    loss  = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / last_loss
    return float(100 - (100 / (1 + rs)))


def _collect_indicators(ticker: str) -> dict | None:
    """ticker → 지표 dict. yfinance 호출 실패 시 None.

    반환 키:
      cur, prev, high_52w, low_52w, cur_volume, avg_volume_20, rsi_14
    부족한 항목은 None.
    """
    try:
        hist = yf.Ticker(ticker).history(period='1y')
    except Exception as e:
        log.warning("alert price 조회 실패 %s: %s", ticker, e)
        return None
    if hist is None or hist.empty:
        return None

    closes = hist['Close']
    vols   = hist['Volume']
    cur    = float(closes.iloc[-1])
    prev   = float(closes.iloc[-2]) if len(closes) >= 2 else None

    high_52w = float(closes.max()) if len(closes) > 0 else None
    low_52w  = float(closes.min()) if len(closes) > 0 else None

    cur_vol = float(vols.iloc[-1]) if len(vols) > 0 else None
    avg_vol = float(vols.iloc[-20:].mean()) if len(vols) >= 20 else None

    return {
        'cur':            cur,
        'prev':           prev,
        'high_52w':       high_52w,
        'low_52w':        low_52w,
        'cur_volume':     cur_vol,
        'avg_volume_20':  avg_vol,
        'rsi_14':         _rsi(closes),
    }


def _is_triggered(alert: dict, ind: dict) -> bool:
    t = alert.get('type')
    v = alert.get('value')
    cur = ind.get('cur')
    if cur is None:
        return False

    if t == 'above':
        return v is not None and cur >= v
    if t == 'below':
        return v is not None and cur <= v

    prev = ind.get('prev')
    if t == 'pct_up':
        return v is not None and prev is not None and (cur / prev - 1) * 100 >= v
    if t == 'pct_down':
        return v is not None and prev is not None and (cur / prev - 1) * 100 <= -v

    if t == '52w_high':
        h = ind.get('high_52w')
        return h is not None and cur >= h
    if t == '52w_low':
        l = ind.get('low_52w')
        return l is not None and cur <= l

    if t == 'volume_spike':
        cv = ind.get('cur_volume')
        av = ind.get('avg_volume_20')
        return v is not None and cv is not None and av is not None and av > 0 and cv >= av * v

    if t == 'rsi_above':
        r = ind.get('rsi_14')
        return v is not None and r is not None and r > v
    if t == 'rsi_below':
        r = ind.get('rsi_14')
        return v is not None and r is not None and r < v

    return False


def check_alerts() -> list[dict]:
    """활성 알림 전체 검사. 트리거된 항목 반환(파일에서도 제거).

    각 트리거 항목엔 indicators 가 첨부되어 메시지 포맷에 사용.
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
        ind = _collect_indicators(ticker)
        if ind is None:
            continue
        for a in group:
            if _is_triggered(a, ind):
                a['indicators'] = ind
                triggered.append(a)
                triggered_ids.add(a['id'])

    if triggered_ids:
        survivors = [a for a in alerts if a.get('id') not in triggered_ids]
        save_alerts(survivors)
    return triggered
