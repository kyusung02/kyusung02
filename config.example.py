"""
config.example.py - 네모 봇 설정 템플릿
========================================
이 파일을 복사하여 config.py 로 이름을 바꾸고 실제 값을 입력하세요.

    cp config.example.py config.py

config.py 는 .gitignore 에 등록되어 있어 저장소에 커밋되지 않습니다.
VM에 배포할 때는 환경변수로 주입하거나 VM에 직접 파일을 생성하세요.

환경변수 설정 방법 (권장):
    # systemd 서비스에 직접 추가
    sudo systemctl edit nemo-bot
    # 아래 내용 입력:
    [Service]
    Environment="TELEGRAM_API_ID=12345678"
    Environment="TELEGRAM_API_HASH=abcdef..."
    ...

    # 또는 /etc/environment 에 추가 (재부팅 필요)
    echo 'TELEGRAM_API_ID=12345678' | sudo tee -a /etc/environment
"""

import os

# ==========================================
# 텔레그램 사용자 계정 (Telethon)
# ==========================================
# my.telegram.org → 로그인 → API Development Tools 에서 확인
# App api_id (숫자)
TELEGRAM_API_ID   = int(os.environ.get("TELEGRAM_API_ID", "0"))
# App api_hash (32자리 문자열)
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")

# ==========================================
# 텔레그램 봇
# ==========================================
# @BotFather 에서 /newbot 명령으로 발급받은 토큰
# 예) 1234567890:ABCDEFGabcdefg-XXXXXXXXXXXXXXXX
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# 봇이 분석 결과를 전송할 내 텔레그램 계정 ID (숫자)
# 확인 방법: @userinfobot 에게 아무 메시지 전송 → "Id: 123456789" 응답
MY_TELEGRAM_ID = int(os.environ.get("MY_TELEGRAM_ID", "0"))

# ==========================================
# API 키
# ==========================================
# Google Gemini API 키 - 뉴스 요약 및 재무 분석에 사용
# 발급: https://aistudio.google.com/app/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 금감원 DART 오픈API 키 - 재무제표 및 공시 조회에 사용
# 발급: https://opendart.fss.or.kr → 로그인 → API 신청
DART_API_KEY = os.environ.get("DART_API_KEY", "")

# ==========================================
# Telethon StringSession (선택사항)
# ==========================================
# VM 등 비대화형 환경에서 .session 파일 대신 문자열 세션을 사용하려면 설정.
# 비워두면 ./user_session.session / ./bot_session.session 파일을 사용.
TELEGRAM_USER_SESSION = os.environ.get("TELEGRAM_USER_SESSION", "")
TELEGRAM_BOT_SESSION  = os.environ.get("TELEGRAM_BOT_SESSION", "")

# ==========================================
# 채널 모니터링 설정
# ==========================================
# 자동 요약할 텔레그램 채널 목록 (쉼표로 구분)
# 채널 접근 방법: user_client 가 해당 채널에 가입된 계정으로 접속해야 함
# 예) WATCH_CHANNELS=@invest_news,@stock_channel,@finance_talk
_watch_raw = os.environ.get("WATCH_CHANNELS", "")
WATCH_CHANNELS = [ch.strip() for ch in _watch_raw.split(",") if ch.strip()]

# 채널 요약 서비스 활성화 여부 (기본 비활성)
# 비활성 시 user_client에 on_channel_msg 핸들러를 등록하지 않아
# 모니터링 채널 메시지가 와도 요약 실행되지 않음. 코드/명령어는 그대로 유지.
# 다시 켜려면: CHANNEL_SUMMARY_ENABLED=true
CHANNEL_SUMMARY_ENABLED = os.environ.get("CHANNEL_SUMMARY_ENABLED", "false").lower() == "true"

# ==========================================
# 경로 설정
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# PDF 등 첨부파일 임시 저장 경로 (Gemini 분석 후 자동 삭제)
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 차트 PNG 임시 저장 경로 (Telegram 전송 후 자동 삭제)
CHARTS_DIR = os.environ.get("CHARTS_DIR", os.path.join(BASE_DIR, "charts"))
os.makedirs(CHARTS_DIR, exist_ok=True)

# 영속 데이터 (watchlist, channels, seen_filings) 저장 경로
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
os.makedirs(DATA_DIR, exist_ok=True)

# ==========================================
# DART 공시 알림 키워드
# ==========================================
# 관심종목 공시 자동 감지 시 이 키워드가 제목에 포함되면 🚨 표시.
DART_ALERT_KEYWORDS = [
    '잠정실적', '연결실적', '영업실적', '매출액', '영업이익',
    '시설투자', '내부자', '자기주식', '최대주주', '유상증자',
    '무상증자', '합병', '분할', '전환사채', '교환사채',
    '임원변동', '특수관계인', '주식매수선택권',
]
