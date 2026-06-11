"""import 스모크 테스트 — main.py 와 전체 모듈 그래프가 import 가능한지 검증.

py_compile이 못 잡는 순환 import·이름 오류를 배포 전에 로컬에서 잡는다.
config/OpenDartReader를 스텁으로 주입하므로 네트워크·실제 키 불필요.
pytest 수집 대상이 아님(파일명 test_* 아님) — 직접 실행: python tests/smoke_import.py
"""
import os
import sys
import types
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── config 스텁 ──────────────────────────────────────────────────────────────
_tmp = tempfile.mkdtemp(prefix="nemo_smoke_")
cfg = types.ModuleType("config")
cfg.DATA_DIR = _tmp
cfg.DOWNLOAD_DIR = os.path.join(_tmp, "dl")
cfg.CHARTS_DIR = os.path.join(_tmp, "ch")
cfg.WATCH_CHANNELS = []
cfg.TELEGRAM_API_ID = 1
cfg.TELEGRAM_API_HASH = "0" * 32
cfg.TELEGRAM_USER_SESSION = ""
cfg.TELEGRAM_BOT_SESSION = ""
cfg.TELEGRAM_BOT_TOKEN = "1:x"
cfg.MY_TELEGRAM_ID = 1
cfg.BROADCAST_ID = 1
cfg.DART_ALERT_KEYWORDS = ["유상증자"]
cfg.GEMINI_API_KEY = "x"
cfg.DART_API_KEY = "x"
sys.modules["config"] = cfg


# ── OpenDartReader 스텁 (실키 없이는 init에서 corp_codes 다운로드 시도) ───────
class _FakeDart:
    corp_codes = None

    def list(self, *a, **k):
        return None


class _CallableModule(types.ModuleType):
    def __call__(self, *a, **k):
        return _FakeDart()


sys.modules["OpenDartReader"] = _CallableModule("OpenDartReader")

# telethon 세션 파일이 repo에 생기지 않게 임시 폴더에서 실행
os.chdir(_tmp)

import main  # noqa: E402,F401 — import 자체가 테스트

print("smoke import ok — main.py 및 전체 모듈 그래프 정상")
