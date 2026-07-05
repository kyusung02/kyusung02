"""OpenDartReader 타임아웃 주입 패치(services.stock._patch_opendart_timeout) 검증.

실제 OpenDartReader/네트워크 없이, requests 참조를 가진 가짜 모듈을 sys.modules에
꽂아 패치가 timeout을 주입하는지 확인한다. conftest가 OpenDartReader를 스텁으로
주입하므로 services.stock import 자체는 네트워크를 타지 않는다.
"""
import sys
import types

import requests

# 로컬/CI엔 OpenDartReader 미설치 — smoke_import.py와 같은 방식으로 스텁 주입
# (services.stock이 import 시점에 모듈 자체를 OpenDartReader(DART_API_KEY)로 호출하므로
#  callable 모듈이어야 한다)
if "OpenDartReader" not in sys.modules:
    class _CallableModule(types.ModuleType):
        def __call__(self, *a, **kw):
            return None

    sys.modules["OpenDartReader"] = _CallableModule("OpenDartReader")


def _make_fake_module(name):
    mod = types.ModuleType(name)
    mod.requests = requests  # OpenDartReader 소스처럼 'import requests' 한 상태 재현
    sys.modules[name] = mod
    return mod


def test_patch_injects_timeout(monkeypatch):
    from services.stock import _patch_opendart_timeout

    fake = _make_fake_module("OpenDartReader.fake_submodule")
    try:
        recorded = {}

        def _fake_get(url, **kwargs):
            recorded.update(kwargs)
            return "ok"

        monkeypatch.setattr(requests, "get", _fake_get)
        _patch_opendart_timeout(seconds=30)

        # 패치 후 모듈의 requests.get 호출에 timeout이 자동으로 붙어야 한다
        assert fake.requests is not requests
        assert fake.requests.get("http://example.invalid") == "ok"
        assert recorded.get("timeout") == 30
    finally:
        sys.modules.pop("OpenDartReader.fake_submodule", None)


def test_patch_respects_explicit_timeout(monkeypatch):
    from services.stock import _patch_opendart_timeout

    fake = _make_fake_module("OpenDartReader.fake_submodule2")
    try:
        recorded = {}

        def _fake_post(url, **kwargs):
            recorded.update(kwargs)
            return "ok"

        monkeypatch.setattr(requests, "post", _fake_post)
        _patch_opendart_timeout(seconds=30)

        # 호출자가 명시한 timeout은 덮어쓰지 않는다
        fake.requests.post("http://example.invalid", timeout=5)
        assert recorded.get("timeout") == 5
    finally:
        sys.modules.pop("OpenDartReader.fake_submodule2", None)


def test_patch_skips_non_requests_modules():
    from services.stock import _patch_opendart_timeout

    # requests 참조가 없는(스텁 포함) 모듈은 건드리지 않고 조용히 지나가야 한다
    _patch_opendart_timeout(seconds=30)  # 예외 없이 통과하면 성공
