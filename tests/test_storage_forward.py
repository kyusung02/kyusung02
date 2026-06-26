"""storage.py — 채널 포워딩 설정 영속화."""
import os
import pytest
import storage


@pytest.fixture(autouse=True)
def clean_forward():
    for p in (storage.FORWARD_PATH, storage.FORWARD_SEEN_PATH):
        if os.path.exists(p):
            os.remove(p)
    yield


def test_empty_config_default():
    cfg = storage.load_forward_config()
    assert cfg == {"target": None, "sources": [], "dedup": True}


def test_set_target_normalized():
    cfg = storage.set_forward_target('mychannel')
    assert cfg["target"] == '@mychannel'
    cfg = storage.set_forward_target('-1001234567890')
    assert cfg["target"] == -1001234567890


def test_add_remove_source_roundtrip():
    ok, cfg = storage.add_forward_source('@news_channel')
    assert ok and cfg["sources"] == ['@news_channel']

    # 중복 추가 거부
    ok, _ = storage.add_forward_source('news_channel')  # normalize 후 동일
    assert ok is False

    # 숫자 ID 채널 추가 — int로 저장/로드
    ok, cfg = storage.add_forward_source('-1009999')
    assert ok and -1009999 in cfg["sources"]

    # 재로드해도 타입 유지 (JSON round-trip)
    cfg2 = storage.load_forward_config()
    assert cfg2["sources"] == ['@news_channel', -1009999]

    ok, cfg = storage.remove_forward_source('@news_channel')
    assert ok and cfg["sources"] == [-1009999]


def test_remove_missing_source():
    ok, _ = storage.remove_forward_source('@ghost')
    assert ok is False


def test_clear_target_keeps_sources():
    storage.set_forward_target('@mychannel')
    storage.add_forward_source('@src')
    cfg = storage.clear_forward_target()
    assert cfg["target"] is None
    assert cfg["sources"] == ['@src']


# ── 중복 필터 플래그 ─────────────────────────────────────────────────────────

def test_dedup_flag_default_on_and_toggle():
    assert storage.load_forward_config()["dedup"] is True
    cfg = storage.set_forward_dedup(False)
    assert cfg["dedup"] is False
    assert storage.load_forward_config()["dedup"] is False
    storage.set_forward_dedup(True)
    assert storage.load_forward_config()["dedup"] is True


def test_dedup_flag_survives_other_edits():
    storage.set_forward_dedup(False)
    storage.set_forward_target('@mychannel')   # 다른 설정 변경이 플래그를 되돌리면 안 됨
    storage.add_forward_source('@src')
    assert storage.load_forward_config()["dedup"] is False


def test_legacy_config_without_dedup_defaults_on():
    # dedup 키 없는 구버전 forward.json — 켜짐으로 간주해야 함
    storage._atomic_json_save(storage.FORWARD_PATH,
                              {"target": "@c", "sources": ["@a"]},
                              ensure_ascii=False, indent=2)
    assert storage.load_forward_config()["dedup"] is True


# ── 중복 지문 저장소 ─────────────────────────────────────────────────────────

def test_forward_dup_detects_repeat():
    t = 1_000_000.0
    assert storage.check_and_mark_forward_dup(["fwd:-100123:55"], now=t) is False  # 처음
    assert storage.check_and_mark_forward_dup(["fwd:-100123:55"], now=t + 10) is True  # 재방문=중복


def test_forward_dup_any_fingerprint_match():
    t = 2_000_000.0
    storage.check_and_mark_forward_dup(["txt:aaa", "url:ex.com/1"], now=t)
    # 일부 지문만 겹쳐도 중복으로 판정
    assert storage.check_and_mark_forward_dup(["fwd:x:1", "url:ex.com/1"], now=t + 5) is True


def test_forward_dup_empty_fingerprints_never_dup():
    # 식별 단서 없는 메시지(지문 0개)는 항상 전달(False)
    assert storage.check_and_mark_forward_dup([], now=3_000_000.0) is False
    assert storage.check_and_mark_forward_dup(None, now=3_000_000.0) is False


def test_forward_dup_ttl_expiry():
    t = 4_000_000.0
    storage.check_and_mark_forward_dup(["fwd:1:1"], now=t)
    later = t + storage._FORWARD_SEEN_TTL + 1   # TTL 경과 후엔 잊혀져 다시 새 글
    assert storage.check_and_mark_forward_dup(["fwd:1:1"], now=later) is False


def test_forward_dup_caps_size():
    base = 5_000_000.0
    for i in range(storage._MAX_FORWARD_SEEN + 50):
        storage.check_and_mark_forward_dup([f"fwd:cap:{i}"], now=base + i)
    seen = storage.load_forward_seen()
    assert len(seen) <= storage._MAX_FORWARD_SEEN
    # 가장 최근 지문은 남아 있어야 함
    last = storage._MAX_FORWARD_SEEN + 49
    assert f"fwd:cap:{last}" in seen
