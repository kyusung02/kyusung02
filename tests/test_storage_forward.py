"""storage.py — 채널 포워딩 설정 영속화."""
import os
import pytest
import storage


@pytest.fixture(autouse=True)
def clean_forward():
    if os.path.exists(storage.FORWARD_PATH):
        os.remove(storage.FORWARD_PATH)
    yield


def test_empty_config_default():
    cfg = storage.load_forward_config()
    assert cfg == {"target": None, "sources": []}


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
