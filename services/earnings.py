"""
어닝(실적) 데이터 — yfinance Ticker.earnings_dates 활용.

기능:
- get_earnings_history(ticker): 과거 어닝 히스토리 (EPS 추정 vs 실제, 서프라이즈%)
  + 다가오는 예정일
- get_upcoming_earnings_for(tickers, within_days): 여러 ticker 의 1주 내 어닝 예정만 추출
- format_earnings_message: 단일 종목 어닝 메시지 포맷

한계: yfinance 의 earnings_dates 는 미국 상장사 위주. 국내 종목은 거의 빈 응답.
국내는 DART 정기보고서 + 네이버 리서치 컨센서스 스크래핑이 필요(다음 단계).
"""
import math
import logging
from datetime import date, timedelta
import yfinance as yf

log = logging.getLogger(__name__)


def _is_nan(x) -> bool:
    if x is None:
        return True
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False


def _safe_float(x) -> float | None:
    if _is_nan(x):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def get_earnings_history(ticker: str, history_limit: int = 6) -> dict | None:
    """단일 ticker 의 어닝 데이터.

    반환: {ticker, upcoming: [...], history: [...]}
      각 항목: {date, eps_est, eps_actual, surprise_pct}
    yfinance 가 데이터 없거나 빈 DataFrame 이면 None.
    """
    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
    except Exception as e:
        log.warning("earnings_dates 조회 실패 %s: %s", ticker, e)
        return None
    if df is None or df.empty:
        return None

    df = df.sort_index(ascending=False)
    today = date.today()
    upcoming: list[dict] = []
    history:  list[dict] = []

    for ts, row in df.iterrows():
        # 컬럼명은 yfinance 버전에 따라 변동 가능 — 'Surprise(%)' 또는 'Surprise (%)'
        est_val      = row.get('EPS Estimate')
        actual_val   = row.get('Reported EPS')
        surprise_val = row.get('Surprise(%)', row.get('Surprise (%)'))

        item = {
            'date':         ts.strftime('%Y-%m-%d'),
            'eps_est':      _safe_float(est_val),
            'eps_actual':   _safe_float(actual_val),
            'surprise_pct': _safe_float(surprise_val),
        }
        d = ts.date() if hasattr(ts, 'date') else date.fromisoformat(item['date'])
        if d > today:
            upcoming.append(item)
        else:
            history.append(item)

    upcoming.sort(key=lambda x: x['date'])
    return {
        'ticker':   ticker,
        'upcoming': upcoming[:3],
        'history':  history[:history_limit],
    }


def get_upcoming_earnings_for(tickers: list[str], within_days: int = 7) -> list[dict]:
    """여러 ticker 중 within_days 내 어닝 예정만 반환.

    각 dict: {ticker, name(=ticker), date, eps_est, days_until}
    name 은 호출자가 보유종목/관심종목 매핑으로 덮어쓸 것.
    """
    today = date.today()
    cutoff = today + timedelta(days=within_days)
    out: list[dict] = []

    for tk in tickers:
        data = get_earnings_history(tk, history_limit=0)
        if not data:
            continue
        for u in data.get('upcoming', []):
            try:
                d = date.fromisoformat(u['date'])
            except ValueError:
                continue
            if today < d <= cutoff:
                out.append({
                    'ticker':     tk,
                    'date':       u['date'],
                    'eps_est':    u.get('eps_est'),
                    'days_until': (d - today).days,
                })

    out.sort(key=lambda x: x['date'])
    return out


def format_earnings_message(name: str, ticker: str, data: dict) -> str:
    """단일 종목 어닝 결과 → 텔레그램 메시지."""
    lines = [f"📅 **{name}** (`{ticker}`) — 어닝 히스토리", ""]

    upcoming = data.get('upcoming') or []
    if upcoming:
        lines.append("■ 다가오는 어닝")
        for item in upcoming:
            est = item.get('eps_est')
            est_str = f"  (EPS est ${est:.2f})" if est is not None else ""
            lines.append(f"- {item['date']}{est_str}")
        lines.append("")

    history = data.get('history') or []
    if history:
        lines.append("■ 최근 어닝 (실제 vs 추정)")
        for item in history:
            est = item.get('eps_est')
            act = item.get('eps_actual')
            surp = item.get('surprise_pct')

            est_str = f"${est:.2f}" if est is not None else "-"
            act_str = f"${act:.2f}" if act is not None else "-"

            if surp is not None:
                arrow = '▲' if surp >= 0 else '▼'
                sign  = '+' if surp >= 0 else ''
                surp_str = f"  {arrow} {sign}{surp:.1f}%"
            else:
                surp_str = ""
            lines.append(f"- {item['date']}: est {est_str} / 실제 {act_str}{surp_str}")
        lines.append("")
        beats = sum(1 for x in history if (x.get('surprise_pct') or 0) > 0)
        lines.append(f"_(최근 {len(history)}회 중 컨센서스 상회 {beats}회)_")

    if not upcoming and not history:
        lines.append("(어닝 데이터 없음 — 국내 종목 또는 소형주는 미지원일 수 있음)")

    return "\n".join(lines)


def format_upcoming_briefing(upcoming: list[dict], name_map: dict[str, str]) -> str:
    """모닝 시황 첨부용 — 1주 내 어닝 예정 요약."""
    if not upcoming:
        return ""
    lines = ["📅 **이번 주 어닝 예정** (보유·관심 종목)", ""]
    for u in upcoming:
        tk      = u['ticker']
        nm      = name_map.get(tk, tk)
        d_until = u['days_until']
        marker  = "🔥 오늘" if d_until == 0 else f"D-{d_until}"
        est     = u.get('eps_est')
        est_str = f"  (EPS est ${est:.2f})" if est is not None else ""
        lines.append(f"- {marker} ({u['date']}) **{nm}** (`{tk}`){est_str}")
    return "\n".join(lines)
