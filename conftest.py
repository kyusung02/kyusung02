"""pytest 공용 설정 — config 스텁 주입.

config.py는 git 제외 파일(실제 API 키 보유)이라 테스트 환경에 없을 수 있다.
테스트는 항상 임시 디렉토리를 DATA_DIR로 쓰는 스텁 config를 주입해
실제 운영 데이터(portfolio.json 등)를 절대 건드리지 않는다.
"""
import sys
import types
import tempfile

_cfg = types.ModuleType("config")
_cfg.DATA_DIR = tempfile.mkdtemp(prefix="nemo_test_data_")
_cfg.WATCH_CHANNELS = []
_cfg.DART_API_KEY = "test-key-not-real"  # services.stock import용 (스텁 OpenDartReader에 전달될 뿐)
sys.modules["config"] = _cfg
