# 네모 봇 — Claude 코드 작성 규칙

코드를 수정하거나 새로 짤 때마다 반드시 다음을 해줘:

1. 수정한 파일에서 import가 다 맞는지 확인
2. 함수명/변수명 오타 없는지 확인
3. 기존 코드와 충돌하는 부분 없는지 확인
4. 실제로 실행 가능한지 python -m py_compile로 문법 검사
5. 검토 결과를 마지막에 한국어로 요약해줘

## 추가 규칙

- `handlers/` 파일 수정할 때는 `main.py`의 라우팅과 연결이 맞는지 확인
- Gemini API 호출 부분은 예외처리(try/except)가 있는지 확인
- 스케줄러 추가할 때는 기존 스케줄과 시간 충돌 없는지 확인

## 현재 스케줄러 현황 (main.py)

| 시간 | 기능 |
|------|------|
| 매일 07:00 | 모닝 시황 브리핑 (`send_us_morning`) |
| 평일 07:30 | 오늘의 증권사 리포트 (`send_daily_research`) |
| 월 08:00 | 반도체 업황 스크리닝 (`send_semi_briefing`) |
| 평일 08:30 | 어닝 D-0/D-1 푸시 (`check_and_notify_imminent_earnings`) |
| 금 09:00 | 장보기 리스트 (`send_weekly_info('shop')`) |
| 목 18:00 | 나들이 추천 (`send_weekly_info('out')`) |
| 평일 09~18시 매 30분 | DART 공시 감지 (`check_dart_watchlist`) |
| 평일 10/12/14/16시 :05 | 국내 섹터 브리핑 (`send_kr_sector_briefing`) |
| 매 5분 | 가격·이벤트 알림 체크 (`check_and_notify_alerts`) |
