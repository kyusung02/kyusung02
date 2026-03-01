"""
config.py - 네모 봇 공통 설정
==============================
환경변수에서 API 키/설정값을 읽습니다.
VM의 /etc/environment 또는 systemd 서비스 파일의 Environment= 항목에 설정하세요.

필수 환경변수:
    TELEGRAM_API_ID     : Telethon 사용자 계정 인증 ID
                          → https://my.telegram.org 에서 발급 (App api_id)
    TELEGRAM_API_HASH   : Telethon 사용자 계정 인증 해시
                          → https://my.telegram.org 에서 발급 (App api_hash)
    TELEGRAM_BOT_TOKEN  : 텔레그램 봇 토큰
                          → @BotFather 에서 /newbot 으로 발급
    MY_TELEGRAM_ID      : 봇이 메시지를 보낼 내 계정 ID (숫자)
                          → @userinfobot 에게 메시지 보내면 확인 가능
    GEMINI_API_KEY      : Google Gemini API 키 (뉴스 요약 / 재무 분석)
                          → https://aistudio.google.com/app/apikey 에서 발급
    DART_API_KEY        : 금감원 DART 오픈API 키 (재무제표 조회)
                          → https://opendart.fss.or.kr 에서 발급
    WATCH_CHANNELS      : 자동 요약할 텔레그램 채널 목록 (쉼표 구분)
                          예) WATCH_CHANNELS=@channel1,@channel2

선택 환경변수:
    DOWNLOAD_DIR        : PDF 등 첨부파일 임시 저장 경로 (기본: bot/downloads)
    MARKET_OPEN_HOUR    : 장 시작 시각 KST (기본: 9)
    MARKET_CLOSE_HOUR   : 장 마감 시각 KST (기본: 16)
"""

import os

# ── 텔레그램 사용자 계정 (Telethon) ──────────────────────────────────────
# my.telegram.org → API Development Tools 에서 확인
TELEGRAM_API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# ── 텔레그램 봇 ──────────────────────────────────────────────────────────
# @BotFather 에서 발급한 봇 토큰
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# ── Telethon StringSession (파일 락 없이 세션 유지) ───────────────────────
# gen_session.py 를 실행해 얻은 문자열을 .env 에 저장하세요.
# 비어 있으면 SQLite 파일 세션(user_session.session)을 사용합니다.
TELEGRAM_USER_SESSION = os.environ.get("TELEGRAM_USER_SESSION", "")
TELEGRAM_BOT_SESSION  = os.environ.get("TELEGRAM_BOT_SESSION", "")

# 봇이 요약/분석 결과를 전송할 내 텔레그램 계정 ID (숫자)
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))

# ── 기존 호환성 별칭 ──────────────────────────────────────────────────────
TELEGRAM_CHAT_ID   = int(os.environ.get("INVEST_CHAT_ID", str(MY_TELEGRAM_ID)))

# ── API 키 ───────────────────────────────────────────────────────────────
# 금감원 DART 오픈API: https://opendart.fss.or.kr/intro/main.do
DART_API_KEY      = os.environ.get("DART_API_KEY", "")
# Google Gemini API: https://aistudio.google.com/app/apikey
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
# Anthropic Claude API (향후 확장용)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ── 경로 ─────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(BASE_DIR, "data")
CHARTS_DIR    = os.path.join(BASE_DIR, "charts")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
DB_PATH       = os.path.join(DATA_DIR, "stocks.db")

for _d in (DATA_DIR, CHARTS_DIR, TEMPLATES_DIR):
    os.makedirs(_d, exist_ok=True)

# ── 채널 모니터링 ─────────────────────────────────────────────────────────
# 자동 요약할 텔레그램 채널 목록 (쉼표 구분 문자열 → 리스트로 변환)
# 예) WATCH_CHANNELS=@invest_news,@stock_talk
_watch_raw = os.environ.get("WATCH_CHANNELS", "")
WATCH_CHANNELS = [ch.strip() for ch in _watch_raw.split(",") if ch.strip()]

# ── 파일 저장 경로 ────────────────────────────────────────────────────────
# PDF 등 첨부파일 임시 저장 디렉터리 (분석 후 Gemini Files API에서 삭제됨)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))

# ── 장 운영 시간 (KST) ────────────────────────────────────────────────────
MARKET_OPEN_HOUR  = int(os.environ.get("MARKET_OPEN_HOUR", "9"))
MARKET_CLOSE_HOUR = int(os.environ.get("MARKET_CLOSE_HOUR", "16"))

# ── DART 공시 필터 키워드 ─────────────────────────────────────────────────
# 이 키워드가 포함된 공시만 알림 전송 (dart_monitor.py에서 사용)
DART_ALERT_KEYWORDS = [
    # 실적 (가장 중요)
    "잠정실적", "실적공시", "영업(잠정)",
    # 지배구조 변동
    "합병", "분할", "주요사항보고", "최대주주변경",
    # 투자/자산
    "시설투자", "유형자산취득", "타법인주식취득",
    # 내부자 수급
    "주식대량보유", "임원·주요주주", "자기주식취득", "자기주식처분",
    # 자금조달
    "유상증자", "전환사채",
    # 위험 신호
    "관리종목", "상장폐지", "감사의견",
]
