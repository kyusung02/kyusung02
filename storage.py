"""
영속 데이터 관리 — watchlist, channels, seen_filings (JSON 파일 기반)
"""
import os
import json
import tempfile
import logging
from config import DATA_DIR, WATCH_CHANNELS

log = logging.getLogger(__name__)


def _atomic_json_save(path: str, data, **kwargs):
    """임시 파일에 쓴 뒤 rename — 프로세스 크래시 시 파일 손상 방지."""
    dir_name = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, **kwargs)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


WATCHLIST_PATH    = os.path.join(DATA_DIR, "watchlist.json")
CHANNELS_PATH     = os.path.join(DATA_DIR, "channels.json")
SEEN_FILINGS_PATH = os.path.join(DATA_DIR, "seen_filings.json")
PORTFOLIO_PATH    = os.path.join(DATA_DIR, "portfolio.json")
ALERTS_PATH         = os.path.join(DATA_DIR, "alerts.json")
SEEN_EARNINGS_PATH  = os.path.join(DATA_DIR, "seen_earnings.json")
_MAX_SEEN = 1000
_MAX_SEEN_EARNINGS = 500


def _parse_channel(ch):
    """채널 식별자를 Telethon이 인식할 수 있는 형태로 변환합니다."""
    s = str(ch).strip()
    try:
        return int(s)
    except ValueError:
        return s


# ── 관심종목 ────────────────────────────────────────────────────────────────

def load_watchlist() -> list[str]:
    if not os.path.exists(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_watchlist(stocks: list[str]):
    _atomic_json_save(WATCHLIST_PATH, stocks, ensure_ascii=False, indent=2)


def add_to_watchlist(name: str) -> tuple[bool, str]:
    stocks = load_watchlist()
    if name in stocks:
        return False, f"'{name}'은(는) 이미 관심종목에 있습니다."
    stocks.append(name)
    save_watchlist(stocks)
    return True, f"✅ '{name}' 관심종목에 추가되었습니다. (총 {len(stocks)}종목)"


def remove_from_watchlist(name: str) -> tuple[bool, str]:
    stocks = load_watchlist()
    if name not in stocks:
        return False, f"'{name}'은(는) 관심종목에 없습니다."
    stocks.remove(name)
    save_watchlist(stocks)
    return True, f"🗑️ '{name}' 관심종목에서 삭제되었습니다. (총 {len(stocks)}종목)"


# ── 모니터링 채널 ────────────────────────────────────────────────────────────

def load_channels() -> list:
    if not os.path.exists(CHANNELS_PATH):
        return [_parse_channel(ch) for ch in WATCH_CHANNELS]
    try:
        with open(CHANNELS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return [_parse_channel(ch) for ch in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_channels(channels: list):
    _atomic_json_save(CHANNELS_PATH, channels, ensure_ascii=False, indent=2)


# ── DART 공시 수신 이력 ───────────────────────────────────────────────────────
# 슬라이싱이 "최신 _MAX_SEEN개만 보존"이 되려면 삽입 순서가 보존되어야 한다.
# set은 순서를 보장하지 않으므로 set 슬라이싱은 비결정적 — list 기반으로 dedup한다.

def load_seen_filings() -> set:
    if not os.path.exists(SEEN_FILINGS_PATH):
        return set()
    try:
        with open(SEEN_FILINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError) as e:
        log.warning("seen_filings 로드 실패 — 빈 set 반환: %s", e)
        return set()


def save_seen_filings(seen):
    """seen이 list(삽입 순서) 또는 set(순서 없음). 가능하면 list로 전달할 것."""
    if isinstance(seen, set):
        items = list(seen)
    else:
        items = list(dict.fromkeys(seen))  # 삽입 순서 유지 dedup
    if len(items) > _MAX_SEEN:
        items = items[-_MAX_SEEN:]
    _atomic_json_save(SEEN_FILINGS_PATH, items)


# ── 포트폴리오 (보유 종목) ───────────────────────────────────────────────────
# 키 = yfinance ticker (예: "005930.KS", "NVDA"). 값 = {name, market, shares, avg_price, first_buy, last_update}
# 매수 시 가중평균 평단가 자동 계산. 매도는 보유 수량만 차감(평단가 유지).

def load_portfolio() -> dict:
    if not os.path.exists(PORTFOLIO_PATH):
        return {}
    try:
        with open(PORTFOLIO_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, IOError) as e:
        log.warning("portfolio 로드 실패 — 빈 dict 반환: %s", e)
        return {}


def save_portfolio(portfolio: dict):
    _atomic_json_save(PORTFOLIO_PATH, portfolio, ensure_ascii=False, indent=2)


def add_buy(ticker: str, name: str, market: str, shares: float, price: float, trade_date: str) -> tuple[bool, str]:
    """매수 추가. 기존 보유 시 가중평균으로 평단가 재계산."""
    if shares <= 0 or price <= 0:
        return False, "수량·가격은 0보다 커야 합니다."
    p = load_portfolio()
    if ticker in p:
        old = p[ticker]
        new_shares = old["shares"] + shares
        new_avg    = (old["shares"] * old["avg_price"] + shares * price) / new_shares
        p[ticker]["shares"]      = new_shares
        p[ticker]["avg_price"]   = new_avg
        p[ticker]["last_update"] = trade_date
        msg = f"✅ {name} {shares}주 추가매수 (평단 {old['avg_price']:,.2f} → {new_avg:,.2f})"
    else:
        p[ticker] = {
            "name":        name,
            "market":      market,
            "shares":      shares,
            "avg_price":   price,
            "first_buy":   trade_date,
            "last_update": trade_date,
        }
        msg = f"✅ {name} 신규매수 {shares}주 @ {price:,.2f}"
    save_portfolio(p)
    return True, msg


def add_sell(ticker: str, shares: float | None = None) -> tuple[bool, str]:
    """매도 처리. shares=None이면 전량 매도. 평단가는 유지(부분 매도 시)."""
    p = load_portfolio()
    if ticker not in p:
        return False, "보유 종목이 아닙니다."
    held = p[ticker]
    name = held["name"]
    if shares is None or shares >= held["shares"]:
        sold = held["shares"]
        del p[ticker]
        msg = f"🗑️ {name} 전량매도 ({sold}주)"
    else:
        held["shares"] -= shares
        msg = f"➖ {name} {shares}주 매도 (잔여 {held['shares']}주)"
    save_portfolio(p)
    return True, msg


# ── D-1 어닝 알림 발송 이력 (중복 방지) ─────────────────────────────────────
# 키 = "{ticker}:{date}". 발송한 알림을 기록해 같은 날 반복 발송을 방지.

def load_seen_earnings() -> set:
    if not os.path.exists(SEEN_EARNINGS_PATH):
        return set()
    try:
        with open(SEEN_EARNINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError) as e:
        log.warning("seen_earnings 로드 실패: %s", e)
        return set()


def save_seen_earnings(seen):
    if isinstance(seen, set):
        items = list(seen)
    else:
        items = list(dict.fromkeys(seen))
    if len(items) > _MAX_SEEN_EARNINGS:
        items = items[-_MAX_SEEN_EARNINGS:]
    _atomic_json_save(SEEN_EARNINGS_PATH, items)


# ── 가격/이벤트 알림 ──────────────────────────────────────────────────────────
# 항목 = {id, ticker, name, market, type, value, note, created_at}
# type: above | below | pct_up | pct_down
# 트리거 시 1회성 — check_alerts 가 매칭 즉시 alerts.json 에서 제거.

def load_alerts() -> list:
    if not os.path.exists(ALERTS_PATH):
        return []
    try:
        with open(ALERTS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError) as e:
        log.warning("alerts 로드 실패 — 빈 list 반환: %s", e)
        return []


def save_alerts(alerts: list):
    _atomic_json_save(ALERTS_PATH, alerts, ensure_ascii=False, indent=2)


def add_alert(alert: dict) -> tuple[bool, int]:
    alerts = load_alerts()
    alerts.append(alert)
    save_alerts(alerts)
    return True, len(alerts)


def remove_alert_by_id(alert_id: str) -> tuple[bool, str]:
    alerts = load_alerts()
    new = [a for a in alerts if a.get('id') != alert_id]
    if len(new) == len(alerts):
        return False, "해당 알림 ID를 찾을 수 없습니다."
    save_alerts(new)
    return True, f"🗑️ 알림 삭제 (남은 {len(new)}개)"


def resolve_ticker_input(name: str, get_kr_ticker_fn, us_map: dict) -> tuple[str | None, str, str]:
    """사용자가 입력한 종목명/티커 → (ticker, market, display_name).

    1) 국내 종목명 (DART/폴백) → .KS/.KQ ticker
    2) US_STOCK_MAP (한글·소문자 매핑) → US ticker
    3) 영문 대문자/티커 직접 입력 → US ticker로 간주
    실패 시 (None, '', '') 반환.
    """
    kr = get_kr_ticker_fn(name)
    if kr:
        return kr, "KR", name
    nl = name.lower().replace(' ', '')
    if nl in us_map:
        ticker = us_map[nl]
        return ticker, "US", ticker
    if name in us_map:
        return us_map[name], "US", us_map[name]
    if name.replace('.', '').replace('-', '').isalpha() and name.isascii():
        return name.upper(), "US", name.upper()
    return None, "", ""
