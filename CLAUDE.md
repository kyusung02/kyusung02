# 네모 봇 — 텔레그램 올인원 자동화 봇

## 프로젝트 개요
- Python + Telethon 듀얼 클라이언트 (user_client + bot_client) + APScheduler
- Gemini API (뉴스 요약, 재무 분석, 자연어 파싱)
- VM(Linux) + systemd 배포, Sentry 에러 트래킹 (선택)

## 파일 구조
```
main.py                 ← 봇 진입점, 명령어 라우팅 (on_bot_msg), 스케줄러 등록
clients.py              ← Telethon user_client + bot_client 초기화
config.py               ← 실제 설정 (git 제외, VM에 직접 관리)
config.example.py       ← 설정 템플릿 (API 키, 경로, 키워드 등)
storage.py              ← JSON 파일 기반 영속 데이터 (watchlist, portfolio, alerts 등)
utils.py                ← reply_chunked, YouTube/웹 유틸

handlers/
  market.py             ← 모닝 시황 (send_us_morning)
  sector.py             ← 국내 섹터 브리핑 (send_kr_sector_briefing)
  semi.py               ← 반도체 업황 스크리닝 (send_semi_briefing)
  report.py             ← 종합 리포트 (handle_report)
  research.py           ← 오늘의 증권사 리포트 (send_daily_research)
  dart.py               ← DART 공시 감지 (check_dart_watchlist)
  earnings.py           ← 어닝 알림 (check_and_notify_imminent_earnings)
  alerts.py             ← 가격·이벤트 알림 (handle_alert_*)
  portfolio.py          ← 포트폴리오 관리 (handle_buy/sell/portfolio/trade)
  life.py               ← 생활 브리핑 (send_weekly_info — 장보기, 나들이)
  channel.py            ← 채널 모니터링·자동 요약

services/
  gemini.py             ← Gemini API 래퍼 (generate_with_retry, analyze_pdf)
  stock.py              ← 주가 조회, 종목 코드 매핑 (yfinance, opendartreader)
  chart.py              ← 차트 PNG 생성 (matplotlib)
  dart_service.py       ← DART 재무제표·공시 조회
  portfolio.py          ← 포트폴리오 데이터 로직
  alerts.py             ← 알림 데이터 로직
  earnings.py           ← 어닝 캘린더 로직
  semi.py               ← 반도체 업황 데이터

data/                   ← 영속 JSON (watchlist, channels, portfolio, alerts 등)
charts/                 ← 차트 PNG 임시 저장 (전송 후 삭제)
downloads/              ← PDF 임시 저장 (분석 후 삭제)
```

## 명령어 목록
| 명령어 | 기능 |
|--------|------|
| `/시황` | 미국 시황 브리핑 |
| `/섹터` | 국내 섹터 브리핑 |
| `/semi`, `/반도체` | 반도체 업황 스크리닝 |
| `/report 종목명` | 종합 리포트 |
| `/오늘리포트` | 오늘 발행 증권사 리포트 |
| `/재무 종목명` | DART 재무 분석 + 차트 |
| `/us 종목명` | 미국 종목 리포트 + 차트 |
| `/공시 종목명` | 최근 DART 공시 3건 |
| `/watch`, `/unwatch`, `/watchlist` | 관심종목 관리 |
| `/거래`, `/buy`, `/sell`, `/portfolio` | 포트폴리오 관리 |
| `/알림`, `/alert`, `/alerts`, `/unalert` | 가격·이벤트 알림 |
| `/earnings`, `/어닝` | 어닝 캘린더 |
| `/장보기`, `/나들이` | 생활 브리핑 |
| `/채널추가`, `/채널삭제`, `/채널목록` | 채널 모니터링 |
| `/keywords` | DART 알림 키워드 확인 |

## 스케줄러 현황 (main.py)
| 시간 | 기능 |
|------|------|
| 매일 07:00 | 모닝 시황 (`send_us_morning`) |
| 평일 07:30 | 증권사 리포트 (`send_daily_research`) |
| 월 08:00 | 반도체 업황 (`send_semi_briefing`) |
| 평일 08:30 | 어닝 D-0/D-1 푸시 (`check_and_notify_imminent_earnings`) |
| 평일 10/12/14/16시 :05 | 국내 섹터 브리핑 (`send_kr_sector_briefing`) |
| 평일 09~18시 매 30분 | DART 공시 감지 (`check_dart_watchlist`) |
| 금 09:00 | 장보기 리스트 (`send_weekly_info('shop')`) |
| 목 18:00 | 나들이 추천 (`send_weekly_info('out')`) |
| 매 5분 | 가격·이벤트 알림 (`check_and_notify_alerts`) |

스케줄 추가 시 기존 시간과 충돌 여부 확인 필수. 섹터(:05)와 DART(:00/:30) 분리됨.

## 코드 작성 규칙

1. 수정한 파일의 import 정합성 확인
2. 함수명/변수명 오타 확인
3. 기존 코드와 충돌 여부 확인
4. `python -m py_compile`로 문법 검사
5. 검토 결과를 한국어로 요약

### 추가 규칙
- `handlers/` 수정 시 `main.py`의 라우팅·import 연결 확인
- Gemini API 호출 부분은 반드시 try/except 예외처리
- `config.py`는 git 제외 파일 — 새 config 변수 추가 시 `config.example.py`와 VM의 `config.py` 양쪽 동기화 필요
- 봇 메시지 핸들러에서 `MY_TELEGRAM_ID` 체크로 본인만 응답

## 필수 환경변수 (config.py / .env)
```
TELEGRAM_API_ID, TELEGRAM_API_HASH   ← Telethon user_client
TELEGRAM_BOT_TOKEN                   ← 봇 클라이언트
MY_TELEGRAM_ID                       ← 응답 대상 계정 ID
GEMINI_API_KEY                       ← Gemini API
DART_API_KEY                         ← 금감원 DART 오픈API
SENTRY_DSN                           ← (선택) Sentry 에러 트래킹
CHANNEL_SUMMARY_ENABLED              ← (선택) 채널 요약 서비스 활성화
```
