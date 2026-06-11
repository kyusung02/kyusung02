"""utils.py — safe_filename (path traversal 방지), PendingStore TTL 검증."""
import utils
from utils import safe_filename, PendingStore


def test_traversal_stripped():
    assert safe_filename('../../etc/passwd') == 'passwd'
    assert '..' not in safe_filename('..\\..\\windows')


def test_dots_only_becomes_unnamed():
    assert safe_filename('..') == 'unnamed'
    assert safe_filename('') == 'unnamed'


def test_korean_name_preserved():
    assert safe_filename('삼성전자') == '삼성전자'
    assert safe_filename('KX하이텍') == 'KX하이텍'


def test_no_separators_in_output():
    for raw in ('a/b/c', 'a\\b\\c', 'US_{x}/../y'):
        out = safe_filename(raw)
        assert '/' not in out and '\\' not in out


# ── PendingStore ────────────────────────────────────────────────────────────

def test_pending_store_put_pop():
    store = PendingStore(ttl_sec=300)
    store.put('k1', {'a': 1})
    item = store.pop('k1')
    assert item is not None and item['a'] == 1
    assert store.pop('k1') is None  # pop은 제거됨


def test_pending_store_ttl_expiry(monkeypatch):
    store = PendingStore(ttl_sec=300)
    store.put('k1', {'a': 1})
    # 시간을 TTL 너머로 전진
    real_time = utils.time.time()
    monkeypatch.setattr(utils.time, 'time', lambda: real_time + 301)
    assert store.pop('k1') is None


def test_pending_store_missing_key():
    assert PendingStore().pop('ghost') is None
