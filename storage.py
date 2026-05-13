"""
영속 데이터 관리 — watchlist, channels, seen_filings (JSON 파일 기반)
"""
import os
import json
import logging
from config import DATA_DIR, WATCH_CHANNELS

log = logging.getLogger(__name__)

WATCHLIST_PATH    = os.path.join(DATA_DIR, "watchlist.json")
CHANNELS_PATH     = os.path.join(DATA_DIR, "channels.json")
SEEN_FILINGS_PATH = os.path.join(DATA_DIR, "seen_filings.json")
_MAX_SEEN = 1000


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
    with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)


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
    with open(CHANNELS_PATH, 'w', encoding='utf-8') as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


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
    with open(SEEN_FILINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(items, f)
