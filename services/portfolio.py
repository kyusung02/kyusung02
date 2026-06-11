"""
포트폴리오 평가 — 현재가 조회 + KRW 환산 + 수익률 계산 + 메시지 포맷.

storage.py 의 load_portfolio() 결과를 받아 종목별 평가/통계를 만들어낸다.
yfinance 가격 조회 실패는 행 단위로 격리하고 전체 평가는 계속한다.
"""
import logging
import yfinance as yf
from utils import kst_today

log = logging.getLogger(__name__)

_USD_KRW_FALLBACK = 1350.0


def get_current_price(ticker: str) -> float | None:
    """현재가(장중) 또는 최근 종가. 실패/빈 응답 시 None.

    국내(.KS/.KQ) 종목은 네이버 우선 — yfinance가 코스닥 소형주에서 가격이
    전일 종가에 머무는 문제 회피(KX하이텍 사례). 실패 시 yfinance 폴백.
    """
    if ticker.endswith(('.KS', '.KQ')):
        # 지연 import — services.stock은 config(DART 키)를 요구하므로
        # 테스트 등 config 없는 환경에서도 이 모듈 import가 깨지지 않게 한다.
        from services.stock import get_kr_price_naver
        price = get_kr_price_naver(ticker)
        if price is not None:
            return price
    try:
        t = yf.Ticker(ticker)
        try:
            price = t.fast_info['lastPrice']
            if price and price > 0:
                return float(price)
        except (KeyError, TypeError, AttributeError):
            pass
        hist = t.history(period='5d')
        if hist.empty:
            return None
        return float(hist['Close'].iloc[-1])
    except Exception as e:
        log.warning("get_current_price(%s) 실패: %s", ticker, e)
        return None


def get_usd_krw() -> float:
    """USD/KRW 환율. 조회 실패 시 보수적 fallback 반환."""
    try:
        t = yf.Ticker("KRW=X")
        try:
            rate = t.fast_info['lastPrice']
            if rate and rate > 0:
                return float(rate)
        except (KeyError, TypeError, AttributeError):
            pass
        hist = t.history(period='5d')
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
    except Exception as e:
        log.warning("USD/KRW 환율 조회 실패, fallback 사용: %s", e)
    return _USD_KRW_FALLBACK


def evaluate_portfolio(portfolio: dict) -> dict:
    """포트폴리오 dict → 평가 결과 dict.

    구조: {rows: [...], totals: {...}, krw_rate, evaluated_at}
    각 row: ticker, name, market, shares, avg_price, cur_price, value_local, pnl_local,
            pct, value_krw, pnl_krw, error(optional)
    """
    krw_rate = get_usd_krw()
    rows = []
    total_cost_krw  = 0.0
    total_value_krw = 0.0

    for ticker, data in portfolio.items():
        shares = data["shares"]
        avg    = data["avg_price"]
        cur    = get_current_price(ticker)

        row = {
            "ticker":    ticker,
            "name":      data.get("name", ticker),
            "market":    data.get("market", "?"),
            "shares":    shares,
            "avg_price": avg,
            "cur_price": cur,
        }
        if cur is None:
            row["error"] = "현재가 조회 실패"
            rows.append(row)
            continue

        cost_local  = shares * avg
        value_local = shares * cur
        pnl_local   = value_local - cost_local
        pct         = (cur / avg - 1) * 100 if avg > 0 else 0.0

        if data.get("market") == "US":
            cost_krw  = cost_local  * krw_rate
            value_krw = value_local * krw_rate
            pnl_krw   = pnl_local   * krw_rate
        else:
            cost_krw  = cost_local
            value_krw = value_local
            pnl_krw   = pnl_local

        row.update({
            "cost_local":  cost_local,
            "value_local": value_local,
            "pnl_local":   pnl_local,
            "pct":         pct,
            "cost_krw":    cost_krw,
            "value_krw":   value_krw,
            "pnl_krw":     pnl_krw,
        })
        rows.append(row)
        total_cost_krw  += cost_krw
        total_value_krw += value_krw

    rows.sort(key=lambda r: r.get("pct", float('-inf')), reverse=True)

    total_pnl_krw = total_value_krw - total_cost_krw
    total_pct     = (total_value_krw / total_cost_krw - 1) * 100 if total_cost_krw > 0 else 0.0

    return {
        "rows":            rows,
        "total_cost_krw":  total_cost_krw,
        "total_value_krw": total_value_krw,
        "total_pnl_krw":   total_pnl_krw,
        "total_pct":       total_pct,
        "krw_rate":        krw_rate,
        "evaluated_at":    kst_today().isoformat(),
    }


def _fmt_won(amount: float) -> str:
    return f"{amount:,.0f}원"


def _arrow(value: float) -> str:
    return "▲" if value >= 0 else "▼"


def _sign(value: float) -> str:
    return "+" if value >= 0 else ""


def format_portfolio_message(eval_result: dict) -> str:
    """평가 결과 → 텔레그램 출력 텍스트 (PDF 양식 호환)."""
    rows  = eval_result["rows"]
    rate  = eval_result["krw_rate"]
    today = eval_result["evaluated_at"]

    if not rows:
        return "💼 포트폴리오가 비어 있습니다.\n\n/buy 종목명 수량 단가  →  매수 기록"

    total_value = eval_result["total_value_krw"]
    total_cost  = eval_result["total_cost_krw"]
    total_pnl   = eval_result["total_pnl_krw"]
    total_pct   = eval_result["total_pct"]

    lines = [
        f"💼 **포트폴리오 평가 — {today}**",
        "",
        "■ 총자산",
        f"- 평가금액: {_fmt_won(total_value)}",
        f"- 매입원가: {_fmt_won(total_cost)}",
        f"- 평가손익: {_arrow(total_pnl)} {_sign(total_pnl)}{_fmt_won(total_pnl)} ({_sign(total_pct)}{total_pct:.2f}%)",
        f"- USD/KRW: {rate:,.2f}원",
        "",
        "■ 종목별 (수익률 순)",
    ]

    for i, row in enumerate(rows, 1):
        name   = row["name"]
        ticker = row["ticker"]
        market = row["market"]
        tag    = f"[{market}]"

        if row.get("error"):
            lines.append(f"{i}. {name} ({ticker}) {tag}  ⚠️ {row['error']}")
            lines.append("")
            continue

        shares = row["shares"]
        avg    = row["avg_price"]
        cur    = row["cur_price"]
        pct    = row["pct"]
        pnl_l  = row["pnl_local"]
        value_krw = row["value_krw"]
        weight = (value_krw / total_value * 100) if total_value > 0 else 0

        if market == "US":
            price_line = f"   {shares}주 × ${avg:,.2f} → ${cur:,.2f}"
            pnl_line   = f"   평가 ${row['value_local']:,.2f} (≈{_fmt_won(value_krw)}) | {_arrow(pnl_l)} {_sign(pnl_l)}${pnl_l:,.2f} ({_sign(pct)}{pct:.2f}%) | 비중 {weight:.1f}%"
        else:
            price_line = f"   {shares}주 × {avg:,.0f}원 → {cur:,.0f}원"
            pnl_line   = f"   평가 {_fmt_won(row['value_local'])} | {_arrow(pnl_l)} {_sign(pnl_l)}{_fmt_won(pnl_l)} ({_sign(pct)}{pct:.2f}%) | 비중 {weight:.1f}%"

        lines.append(f"{i}. {name} ({ticker}) {tag}")
        lines.append(price_line)
        lines.append(pnl_line)
        lines.append("")

    valid = [r for r in rows if "pct" in r]
    if valid:
        best  = max(valid, key=lambda r: r["pct"])
        worst = min(valid, key=lambda r: r["pct"])
        lines.append("■ 통계")
        lines.append(f"- 종목 수: {len(rows)}")
        lines.append(f"- 최고 수익: {best['name']} ({_sign(best['pct'])}{best['pct']:.2f}%)")
        lines.append(f"- 최대 손실: {worst['name']} ({_sign(worst['pct'])}{worst['pct']:.2f}%)")

    return "\n".join(lines)
