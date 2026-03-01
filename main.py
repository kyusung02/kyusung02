"""
네모 봇 (Nemo Bot) - 텔레그램 올인원 자동화 봇
=================================================
이 봇은 다음 기능을 하나의 스크립트로 통합합니다:

1. 채널 모니터링 & 자동 요약
   - 지정된 텔레그램 채널에 새 메시지가 올라오면
   - URL을 추출하고 해당 웹페이지 본문을 가져와
   - Gemini AI로 핵심 3줄 요약 + 시사점을 생성 후 전송

2. 재무 데이터 분석 (/재무 <종목명>)
   - OpenDartReader로 DART 공시 재무제표를 조회
   - 매출액·영업이익·당기순이익을 Gemini에 전달해 투자 분석 리포트 생성

3. DART 공시 조회 (/공시 <종목명>)
   - 해당 종목의 최근 주요 공시 3건을 링크와 함께 제공

4. 모닝 시황 브리핑 (자동 스케줄 + /시황 수동 호출)
   - 매일 07:00 → 미국 3대 지수(S&P/NASDAQ/DOW) + KOSPI/KOSDAQ
   - 원자재(금/WTI), 금리(미국 2Y/10Y), VIX, BTC → Gemini 시황 요약

4-1. 주간 생활 브리핑 (자동 스케줄)
   - 금요일 09:00 → /장보기 : 저당/건강 주말 식단 + 장보기 리스트
   - 목요일 18:00 → /나들이 : 41개월 아이와 가기 좋은 서울/경기 나들이 추천

5. 미국 주식 조회 (/us <종목명 또는 ticker>)
   - yfinance로 미국 상장 종목의 실시간 주가·밸류에이션 정보를 조회
   - 일봉(3개월) + 주봉(5년) 차트 이미지를 함께 전송
   - 한글 종목명(예: 엔비디아) 또는 ticker(예: NVDA) 모두 지원

6. 관심종목 관리 + DART 공시 자동 감지 (/watch, /unwatch, /watchlist)
   - 텔레그램에서 실시간으로 관심종목 추가/삭제
   - bot/data/watchlist.json에 영속 저장 (재시작 후에도 유지)
   - 평일 09:00~18:00 매 30분마다 DART 신규 공시 자동 감지 및 알림
   - 중요 키워드(DART_ALERT_KEYWORDS) 포함 시 🚨, 일반 공시는 📢 구분 전송
   - bot/data/seen_filings.json으로 중복 알림 방지

사용 기술 스택:
  - Telethon  : 텔레그램 클라이언트 & 봇 API
  - Google Gemini (gemini-2.5-flash) : AI 요약/분석
  - OpenDartReader : 금감원 DART 재무 데이터
  - APScheduler : 비동기 주기 작업 스케줄러
  - Pandas : 재무 데이터프레임 처리
  - yfinance : 실시간 주가 및 차트 데이터 (국내·미국)
  - Matplotlib : 주가 차트 이미지 생성
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import os
import logging
import re
import json
import pandas as pd
from datetime import datetime, date
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaDocument
import urllib.request
import urllib.parse
from youtube_transcript_api import YouTubeTranscriptApi
from google import genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import OpenDartReader
import yfinance as yf
import matplotlib
matplotlib.use('Agg')  # 화면 없는 서버 환경용 백엔드
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=2)
# ⚠️ [성능 주의] Gemini SDK의 client.models.generate_content()는 동기(blocking) 함수입니다.
# async 함수 내에서 직접 호출하면 asyncio 이벤트 루프 스레드를 점유해 CPU 사용률이 급증합니다.
# 올바른 처리 방법:
#   loop.run_in_executor(_executor, client.models.generate_content, model, contents)
# 아래 핸들러들(on_channel_msg, on_bot_msg)의 Gemini 호출은 현재 동기 방식으로 실행됩니다.
# CPU 부하가 높을 경우 run_in_executor 로 감싸는 리팩터링을 권장합니다.

# ==========================================
# 1. 로깅 및 기본 설정
# ==========================================
# 실행 시각·레벨·메시지를 포함한 표준 로그 포맷 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# config.py 에서 민감 정보(API 키, 채널 목록 등)를 불러옴
# - TELEGRAM_API_ID / TELEGRAM_API_HASH : Telethon 인증용
# - TELEGRAM_BOT_TOKEN                  : 봇 클라이언트 로그인용
# - MY_TELEGRAM_ID                      : 메시지를 받을 내 계정 ID
# - GEMINI_API_KEY                      : Google Gemini API 인증
# - WATCH_CHANNELS                      : 자동 요약할 채널 목록
# - DOWNLOAD_DIR                        : 파일 저장 디렉터리
# - DART_API_KEY                        : 금감원 DART API 인증
from config import (
    TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_BOT_TOKEN,
    MY_TELEGRAM_ID, GEMINI_API_KEY, WATCH_CHANNELS,
    DOWNLOAD_DIR, DART_API_KEY, CHARTS_DIR, DATA_DIR,
    DART_ALERT_KEYWORDS,
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==========================================
# 관심종목(watchlist) 관리 — bot/data/watchlist.json
# ==========================================
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")


def load_watchlist() -> list[str]:
    """watchlist.json에서 관심종목 리스트를 읽어옵니다."""
    if not os.path.exists(WATCHLIST_PATH):
        return []
    try:
        with open(WATCHLIST_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_watchlist(stocks: list[str]):
    """관심종목 리스트를 watchlist.json에 저장합니다."""
    with open(WATCHLIST_PATH, 'w', encoding='utf-8') as f:
        json.dump(stocks, f, ensure_ascii=False, indent=2)


def add_to_watchlist(name: str) -> tuple[bool, str]:
    """종목 추가. (성공여부, 메시지) 반환."""
    stocks = load_watchlist()
    if name in stocks:
        return False, f"'{name}'은(는) 이미 관심종목에 있습니다."
    stocks.append(name)
    save_watchlist(stocks)
    return True, f"✅ '{name}' 관심종목에 추가되었습니다. (총 {len(stocks)}종목)"


def remove_from_watchlist(name: str) -> tuple[bool, str]:
    """종목 삭제. (성공여부, 메시지) 반환."""
    stocks = load_watchlist()
    if name not in stocks:
        return False, f"'{name}'은(는) 관심종목에 없습니다."
    stocks.remove(name)
    save_watchlist(stocks)
    return True, f"🗑️ '{name}' 관심종목에서 삭제되었습니다. (총 {len(stocks)}종목)"


# ==========================================
# 모니터링 채널 관리 — data/channels.json
# ==========================================
CHANNELS_PATH = os.path.join(DATA_DIR, "channels.json")


def _parse_channel(ch):
    """채널 식별자를 Telethon이 인식할 수 있는 형태로 변환합니다.
    숫자로만 이루어진 문자열(또는 - 포함 숫자)은 int로 변환합니다."""
    s = str(ch).strip()
    try:
        return int(s)
    except ValueError:
        return s


def load_channels() -> list:
    """channels.json에서 모니터링 채널 목록을 읽습니다.
    파일이 없으면 환경변수 WATCH_CHANNELS 값으로 초기화합니다."""
    if not os.path.exists(CHANNELS_PATH):
        return [_parse_channel(ch) for ch in WATCH_CHANNELS]
    try:
        with open(CHANNELS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return [_parse_channel(ch) for ch in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_channels(channels: list[str]):
    """모니터링 채널 목록을 channels.json에 저장합니다."""
    with open(CHANNELS_PATH, 'w', encoding='utf-8') as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


async def update_channel_handler(channels: list[str]):
    """채널 목록 변경 시 user_client 이벤트 핸들러를 재등록합니다."""
    user_client.remove_event_handler(on_channel_msg)
    if channels:
        user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=channels))
        log.info("채널 핸들러 재등록: %s", channels)
    else:
        log.info("모니터링 채널 없음 — 핸들러 비활성화")


# ==========================================
# 관심종목 공시 감지 — 중복 방지용 수신 이력 관리
# ==========================================
SEEN_FILINGS_PATH = os.path.join(DATA_DIR, "seen_filings.json")
_MAX_SEEN = 1000  # 이력 상한선 (오래된 항목 자동 정리)


def load_seen_filings() -> set:
    """이미 알림을 보낸 공시 접수번호(rcept_no) 집합을 불러옵니다."""
    if not os.path.exists(SEEN_FILINGS_PATH):
        return set()
    try:
        with open(SEEN_FILINGS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, IOError):
        return set()


def save_seen_filings(seen: set):
    """수신 이력을 seen_filings.json에 저장합니다. 상한선 초과 시 오래된 항목 제거."""
    items = list(seen)
    if len(items) > _MAX_SEEN:
        items = items[-_MAX_SEEN:]
    with open(SEEN_FILINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(items, f)


# Gemini 및 DART 클라이언트 초기화
client = genai.Client(api_key=GEMINI_API_KEY)
dart = OpenDartReader(DART_API_KEY)

# ==========================================
# 2. AI 프롬프트 정의
# ==========================================
# 각 기능별로 Gemini에 전달할 지시문(system prompt)을 상수로 관리합니다.

# 링크 요약: 웹페이지 본문을 받아 투자 관점 요약 + 시사점 출력
LINK_PROMPT = """당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.
제공된 뉴스/기사 내용을 바탕으로 아래 형식으로 정리하세요.

[네모 봇 인사이트]
■ 핵심 요약 (3줄)
①
②
③

■ 투자 시사점
- 해당 이슈가 업종/기업 실적에 미치는 영향 (긍정/부정/중립)
- 주목해야 할 수혜주 또는 피해주가 있다면 언급

■ 리스크 요인
- 이 뉴스와 관련하여 투자자가 주의해야 할 불확실성 또는 하방 리스크

간결하고 객관적인 팩트 중심으로 서술하세요."""

# 재무 분석: DART 재무 수치를 받아 성장성·수익성·건전성 분석 + 투자 의견 제공
FINANCE_PROMPT = """당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.
아래 제공된 DART 공시 재무 데이터를 분석하여 기관 투자자 대상 리서치 리포트 형식으로 작성하세요.

[재무 트렌드 분석 리포트]

1. 매출 성장성 (Revenue Growth)
   - 전기 대비 매출액 증가율(YoY) 계산 및 성장 동력 해석
   - 성장이 일회성인지 구조적 트렌드인지 판단

2. 수익성 분석 (Profitability)
   - 영업이익률(OPM) 및 순이익률(NPM) 산출
   - 비용 구조 변화 및 레버리지 효과 평가

3. 재무 건전성 (Financial Health)
   - 이익의 질(earning quality): 영업이익과 순이익 gap 분석
   - 특이사항(일회성 이익·손실, 지분법 효과 등) 언급

4. 투자 의견 (Investment View)
   - 종합 평가: 매수(BUY) / 중립(HOLD) / 매도(SELL) 중 하나로 의견 제시
   - 핵심 투자 포인트 2~3가지
   - 주요 모니터링 지표 (다음 분기 체크포인트)

5. 리스크 요인 (Key Risks)
   - 실적 전망을 훼손할 수 있는 매크로·업황·기업 고유 리스크 2~3가지

수치 기반의 객관적 분석을 원칙으로 하며, 단정적 표현 대신 확률적·조건부 표현을 사용하세요."""

# PDF 리포트 분석: 업로드된 주식 리서치 리포트를 전문 애널리스트 관점에서 요약
PDF_PROMPT = """당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.
첨부된 주식 리서치 리포트/투자 보고서를 분석하여 아래 형식으로 정리하세요.

[PDF 리포트 분석]

■ 리포트 개요
- 발행사 / 애널리스트 / 발행일
- 분석 대상 종목 및 목표주가·투자의견 (있을 경우)

■ 핵심 투자 포인트 (3가지)
①
②
③

■ 실적 전망 요약
- 주요 재무 추정치 (매출·영업이익 등) 및 근거

■ 밸류에이션 분석
- 적용 방법론 (PER, PBR, DCF 등) 및 목표주가 산출 근거

■ 리스크 요인
- 투자 의견을 훼손할 수 있는 주요 리스크 2~3가지

■ 총평
- 기존 컨센서스 대비 차별화 포인트 및 주목 이유 한 줄 요약

객관적이고 간결하게 핵심만 서술하세요."""

# 주간 장보기: 41개월 여아 포함 3인 가족 기준 저당/건강 식단 + 장보기 목록 생성
SHOPPING_PROMPT = "41개월 여아가 있는 3인 가족을 위한 주말 저당/건강 식단과 장보기 리스트를 짜주세요."

# 주간 나들이: 아이와 함께할 수 있는 서울/경기 나들이 장소 추천
OUTING_PROMPT = "41개월 아이와 가기 좋은 서울/경기 나들이 장소를 추천하세요."

# ==========================================
# US 종목명(한글/영문) → ticker 매핑
# ==========================================
US_STOCK_MAP = {
    # 반도체
    '엔비디아': 'NVDA', 'nvidia': 'NVDA',
    'AMD': 'AMD', 'amd': 'AMD',
    '인텔': 'INTC', 'intel': 'INTC',
    '퀄컴': 'QCOM', 'qualcomm': 'QCOM',
    '마이크론': 'MU', 'micron': 'MU',
    'TSMC': 'TSM', '대만반도체': 'TSM',
    '브로드컴': 'AVGO', 'broadcom': 'AVGO',
    'ARM': 'ARM', 'arm': 'ARM',
    # 빅테크
    '애플': 'AAPL', 'apple': 'AAPL',
    '마이크로소프트': 'MSFT', 'microsoft': 'MSFT',
    '구글': 'GOOGL', '알파벳': 'GOOGL', 'alphabet': 'GOOGL', 'google': 'GOOGL',
    '아마존': 'AMZN', 'amazon': 'AMZN',
    '메타': 'META', 'meta': 'META',
    '넷플릭스': 'NFLX', 'netflix': 'NFLX',
    # EV/전기차
    '테슬라': 'TSLA', 'tesla': 'TSLA',
    # 금융
    '버크셔': 'BRK-B', 'berkshire': 'BRK-B',
    '비자': 'V', 'visa': 'V',
    '마스터카드': 'MA', 'mastercard': 'MA',
    'JP모건': 'JPM', 'jpmorgan': 'JPM',
    # AI/소프트웨어
    '팔란티어': 'PLTR', 'palantir': 'PLTR',
    '스노우플레이크': 'SNOW', 'snowflake': 'SNOW',
    '세일즈포스': 'CRM', 'salesforce': 'CRM',
    # 기타
    '코인베이스': 'COIN', 'coinbase': 'COIN',
    '코카콜라': 'KO', 'cocacola': 'KO',
    '존슨앤존슨': 'JNJ',
    'ASML': 'ASML', 'asml': 'ASML',
}

# ==========================================
# 주가 / 차트 유틸리티 함수
# ==========================================

def get_kr_ticker(company: str):
    """DART find_corp으로 종목명 → yfinance ticker (예: 005930.KS) 변환"""
    try:
        result = dart.find_corp(company)
        if result is None or result.empty:
            return None
        # KOSPI(Y) / KOSDAQ(K) 상장사 우선 선택
        listed = result[result['corp_cls'].isin(['Y', 'K'])]
        row = listed.iloc[0] if not listed.empty else result.iloc[0]
        stock_code = str(row.get('stock_code', '')).strip()
        if not stock_code or stock_code == 'nan':
            return None
        suffix = '.KS' if row.get('corp_cls') == 'Y' else '.KQ'
        return stock_code + suffix
    except Exception:
        return None


def get_price_info_kr(ticker: str, company: str) -> str:
    """yfinance로 국내 주가 정보 텍스트 생성 (동기 함수 - executor에서 실행)"""
    try:
        hist = yf.Ticker(ticker).history(period='1y')
        if hist.empty:
            return ''
        cur   = hist['Close'].iloc[-1]
        prev  = hist['Close'].iloc[-2] if len(hist) > 1 else cur
        chg   = cur - prev
        chg_p = chg / prev * 100

        w_chg = (cur - hist['Close'].iloc[-5])  / hist['Close'].iloc[-5]  * 100 if len(hist) >= 5  else 0.0
        m_chg = (cur - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20] * 100 if len(hist) >= 20 else 0.0
        ytd   = (cur - hist['Close'].iloc[0])   / hist['Close'].iloc[0]   * 100

        hi52 = hist['High'].max()
        lo52 = hist['Low'].min()
        arrow = '▲' if chg >= 0 else '▼'
        sign  = '+' if chg >= 0 else ''
        return (
            f"💰 **실시간 주가**\n"
            f"현재가: {cur:,.0f}원  {arrow} {sign}{chg:,.0f} ({sign}{chg_p:.1f}%)\n"
            f"금주: {w_chg:+.1f}%  |  금월: {m_chg:+.1f}%  |  YTD: {ytd:+.1f}%\n"
            f"52주 고: {hi52:,.0f}원  |  저: {lo52:,.0f}원"
        )
    except Exception as e:
        return f'(주가 조회 실패: {e})'


def _draw_chart_kr(ticker: str, company: str, path_daily: str, path_weekly: str):
    """일봉(3개월) + 주봉(5년) 차트 파일 저장 (동기 함수 - executor에서 실행)"""
    stock = yf.Ticker(ticker)

    # ── 일봉 3개월 ───────────────────────────────────────────
    df_d = stock.history(period='3mo')
    if not df_d.empty:
        df_d['MA5']  = df_d['Close'].rolling(5).mean()
        df_d['MA20'] = df_d['Close'].rolling(20).mean()
        df_d['MA60'] = df_d['Close'].rolling(60).mean()

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                        gridspec_kw={'height_ratios': [3, 1]})
        fig.suptitle(f'{company} Daily Chart (3M)', fontsize=13)
        ax1.plot(df_d.index, df_d['Close'], linewidth=1.5, color='#1f77b4', label='Close')
        ax1.plot(df_d.index, df_d['MA5'],  linewidth=1, color='orange', alpha=0.9, label='MA5')
        ax1.plot(df_d.index, df_d['MA20'], linewidth=1, color='green',  alpha=0.9, label='MA20')
        ax1.plot(df_d.index, df_d['MA60'], linewidth=1, color='red',    alpha=0.9, label='MA60')
        ax1.legend(loc='upper left', fontsize=8)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        ax1.set_ylabel('Price (KRW)')
        ax1.grid(True, alpha=0.3)

        colors = ['#d32f2f' if c >= o else '#1976d2'
                  for c, o in zip(df_d['Close'], df_d['Open'])]
        ax2.bar(df_d.index, df_d['Volume'], color=colors, alpha=0.7)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        ax2.set_ylabel('Volume')
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(path_daily, dpi=100, bbox_inches='tight')
        plt.close()

    # ── 주봉 5년 ─────────────────────────────────────────────
    df_w = stock.history(period='5y', interval='1wk')
    if not df_w.empty:
        df_w['MA5']   = df_w['Close'].rolling(5).mean()
        df_w['MA20']  = df_w['Close'].rolling(20).mean()
        df_w['MA200'] = df_w['Close'].rolling(200).mean()

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                        gridspec_kw={'height_ratios': [3, 1]})
        fig.suptitle(f'{company} Weekly Chart (5Y)', fontsize=13)
        ax1.plot(df_w.index, df_w['Close'], linewidth=1.5, color='#1f77b4', label='Close')
        ax1.plot(df_w.index, df_w['MA5'],   linewidth=1, color='orange', alpha=0.9, label='MA5')
        ax1.plot(df_w.index, df_w['MA20'],  linewidth=1, color='green',  alpha=0.9, label='MA20')
        ax1.plot(df_w.index, df_w['MA200'], linewidth=1, color='red',    alpha=0.9, label='MA200')
        ax1.legend(loc='upper left', fontsize=8)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y/%m'))
        ax1.set_ylabel('Price (KRW)')
        ax1.grid(True, alpha=0.3)

        colors = ['#d32f2f' if c >= o else '#1976d2'
                  for c, o in zip(df_w['Close'], df_w['Open'])]
        ax2.bar(df_w.index, df_w['Volume'], color=colors, alpha=0.7)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y/%m'))
        ax2.set_ylabel('Volume')
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(path_weekly, dpi=100, bbox_inches='tight')
        plt.close()


def get_us_report_text(query: str) -> str:
    """미국 종목 리포트 텍스트 생성 (동기 함수 - executor에서 실행)"""
    ticker = US_STOCK_MAP.get(query.lower(), US_STOCK_MAP.get(query, query.upper()))
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        hist  = stock.history(period='1y')
        if hist.empty:
            return f"❌ '{query}' 종목을 찾을 수 없습니다. (ticker: {ticker})"

        cur   = hist['Close'].iloc[-1]
        prev  = hist['Close'].iloc[-2] if len(hist) > 1 else cur
        chg   = cur - prev
        chg_p = chg / prev * 100

        w_chg = (cur - hist['Close'].iloc[-5])  / hist['Close'].iloc[-5]  * 100 if len(hist) >= 5  else 0.0
        m_chg = (cur - hist['Close'].iloc[-20]) / hist['Close'].iloc[-20] * 100 if len(hist) >= 20 else 0.0
        ytd   = (cur - hist['Close'].iloc[0])   / hist['Close'].iloc[0]   * 100
        hi52  = hist['High'].max()
        lo52  = hist['Low'].min()

        name      = info.get('longName', ticker)
        sector    = info.get('sector', 'N/A')
        industry  = info.get('industry', 'N/A')
        mkt_cap   = info.get('marketCap', 0) or 0
        per       = info.get('trailingPE') or 0
        fwd_per   = info.get('forwardPE')  or 0
        beta      = info.get('beta')       or 0
        div_yield = (info.get('dividendYield') or 0) * 100

        cap_str = f"${mkt_cap/1e12:.2f}T" if mkt_cap >= 1e12 else f"${mkt_cap/1e9:.1f}B"
        arrow   = '▲' if chg >= 0 else '▼'
        sign    = '+' if chg >= 0 else ''

        # EPS 서프라이즈 (최근 4분기)
        eps_lines = ''
        try:
            cal = stock.earnings_dates
            if cal is not None and not cal.empty:
                recent = cal.dropna(subset=['EPS Estimate', 'Reported EPS']).head(4)
                if not recent.empty:
                    eps_lines = '\n📌 **EPS 서프라이즈 (최근 4분기)**\n'
                    for dt, row in recent.iterrows():
                        est = row['EPS Estimate']
                        act = row['Reported EPS']
                        beat = '✅ Beat' if act >= est else '❌ Miss'
                        eps_lines += f"{str(dt)[:10]} | est ${est:.2f} → actual ${act:.2f}  {beat}\n"
        except Exception:
            pass

        return (
            f"🏢 **{name} ({ticker})**\n"
            f"섹터: {sector} | {industry}\n"
            f"시가총액: {cap_str}\n\n"
            f"💵 **실시간 주가**\n"
            f"현재가: ${cur:.2f}  {arrow} {sign}${chg:.2f} ({sign}{chg_p:.1f}%)\n"
            f"금주: {w_chg:+.1f}%  |  금월: {m_chg:+.1f}%  |  YTD: {ytd:+.1f}%\n"
            f"52W High: ${hi52:.2f}  |  Low: ${lo52:.2f}\n\n"
            f"📊 **밸류에이션**\n"
            f"PER: {per:.1f}x  |  Fwd PER: {fwd_per:.1f}x  |  Beta: {beta:.2f}\n"
            f"배당수익률: {div_yield:.2f}%"
            f"{eps_lines}"
        )
    except Exception as e:
        return f"⚠️ 오류가 발생했습니다: {e}"


def _draw_chart_us(ticker_raw: str, path_daily: str, path_weekly: str):
    """미국 종목 일봉(3개월) + 주봉(5년) 차트 저장 (동기 함수 - executor에서 실행)"""
    ticker = US_STOCK_MAP.get(ticker_raw.lower(), US_STOCK_MAP.get(ticker_raw, ticker_raw.upper()))
    stock  = yf.Ticker(ticker)

    for period, interval, path, fmt, title_suffix in [
        ('3mo', '1d',  path_daily,  '%m/%d',  'Daily (3M)'),
        ('5y',  '1wk', path_weekly, '%Y/%m',  'Weekly (5Y)'),
    ]:
        df = stock.history(period=period, interval=interval)
        if df.empty:
            continue
        ma_short = 5 if interval == '1d' else 5
        ma_mid   = 20
        ma_long  = 60 if interval == '1d' else 200
        df['MA_S'] = df['Close'].rolling(ma_short).mean()
        df['MA_M'] = df['Close'].rolling(ma_mid).mean()
        df['MA_L'] = df['Close'].rolling(ma_long).mean()

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                        gridspec_kw={'height_ratios': [3, 1]})
        fig.suptitle(f'{ticker} {title_suffix}', fontsize=13)
        ax1.plot(df.index, df['Close'], linewidth=1.5, color='#1f77b4', label='Close')
        ax1.plot(df.index, df['MA_S'], linewidth=1, color='orange', alpha=0.9, label=f'MA{ma_short}')
        ax1.plot(df.index, df['MA_M'], linewidth=1, color='green',  alpha=0.9, label=f'MA{ma_mid}')
        ax1.plot(df.index, df['MA_L'], linewidth=1, color='red',    alpha=0.9, label=f'MA{ma_long}')
        ax1.legend(loc='upper left', fontsize=8)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
        ax1.set_ylabel('Price (USD)')
        ax1.grid(True, alpha=0.3)

        colors = ['#d32f2f' if c >= o else '#1976d2'
                  for c, o in zip(df['Close'], df['Open'])]
        ax2.bar(df.index, df['Volume'], color=colors, alpha=0.7)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
        ax2.set_ylabel('Volume')
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(path, dpi=100, bbox_inches='tight')
        plt.close()


def _draw_chart_financials(ticker_sym: str, company: str, path: str):
    """
    yfinance 분기 실적 데이터로 TTM/분기 실적 차트를 생성합니다. (동기 함수 - executor에서 실행)

    Args:
        ticker_sym (str): yfinance ticker (예: '005930.KS', 'AAPL')
        company    (str): 차트 제목에 표시할 종목명
        path       (str): 저장할 PNG 파일 경로

    차트 구성:
        - 3행 subplot: 매출(Revenue) / 영업이익(Operating Income) / 순이익(Net Income)
        - 가로축: 최근 8분기 레이블 (YYYYQQ 형식)
        - 각 막대에 TTM(최근 4분기 합계) 수평 점선 표시
        - 단위: 조원(KRW) 또는 B USD 자동 판별
    """
    try:
        stock = yf.Ticker(ticker_sym)
        df = stock.quarterly_income_stmt  # columns = DatetimeIndex, rows = 계정명
        if df is None or df.empty:
            return

        # 최근 8분기 역순 정렬 (오래된 → 최신)
        df = df.sort_index(axis=1)
        df = df.iloc[:, -8:]

        # 계정명 후보 (yfinance 버전마다 다를 수 있음)
        def _find_row(candidates):
            for name in candidates:
                if name in df.index:
                    return df.loc[name]
            return None

        rev  = _find_row(['Total Revenue', 'Revenue'])
        oi   = _find_row(['Operating Income', 'EBIT', 'Operating Income Loss'])
        ni   = _find_row(['Net Income', 'Net Income Common Stockholders'])

        if rev is None:
            return  # 핵심 데이터 없으면 차트 생략

        # 단위 결정: KRW 종목은 조원, USD는 10억$
        is_krw = ticker_sym.endswith('.KS') or ticker_sym.endswith('.KQ')
        if is_krw:
            unit  = 1e12       # 조원
            ulabel = '조원'
        else:
            unit  = 1e9        # B USD
            ulabel = 'B $'

        # 분기 레이블 (YYYYQQ)
        def _quarter_label(dt):
            q = (dt.month - 1) // 3 + 1
            return f"{dt.year}Q{q}"

        labels = [_quarter_label(c) for c in df.columns]

        # TTM = 최근 4분기 합산
        def _ttm(series):
            if series is None:
                return None
            vals = pd.to_numeric(series, errors='coerce').dropna()
            return vals.iloc[-4:].sum() / unit if len(vals) >= 4 else None

        ttm_rev = _ttm(rev)
        ttm_oi  = _ttm(oi)
        ttm_ni  = _ttm(ni)

        rows_to_plot = [(rev, ttm_rev, '매출액', '#4C72B0'),
                        (oi,  ttm_oi,  '영업이익', '#55A868'),
                        (ni,  ttm_ni,  '순이익',   '#C44E52')]
        rows_to_plot = [(s, t, n, c) for s, t, n, c in rows_to_plot if s is not None]

        n_rows = len(rows_to_plot)
        fig, axes = plt.subplots(n_rows, 1, figsize=(10, 3.2 * n_rows),
                                 gridspec_kw={'hspace': 0.55})
        if n_rows == 1:
            axes = [axes]

        fig.suptitle(f'{company}  분기 실적 (TTM 기준)', fontsize=13, fontweight='bold')

        for ax, (series, ttm_val, name, color) in zip(axes, rows_to_plot):
            vals = pd.to_numeric(series, errors='coerce').fillna(0) / unit
            bar_colors = [color if v >= 0 else '#d62728' for v in vals]
            bars = ax.bar(range(len(labels)), vals, color=bar_colors, alpha=0.85, width=0.6)

            # 막대 값 레이블
            for bar, v in zip(bars, vals):
                if v == 0:
                    continue
                ypos = bar.get_height() if v >= 0 else bar.get_height()
                ax.text(bar.get_x() + bar.get_width() / 2,
                        ypos + (abs(vals.max() - vals.min()) * 0.02),
                        f'{v:.1f}', ha='center', va='bottom', fontsize=7.5)

            # TTM 수평 점선
            if ttm_val is not None:
                ax.axhline(ttm_val, color='orange', linewidth=1.5,
                           linestyle='--', label=f'TTM: {ttm_val:.1f}{ulabel}')
                ax.legend(fontsize=8, loc='upper left')

            ax.set_title(f'{name} ({ulabel})', fontsize=10)
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, fontsize=8, rotation=30)
            ax.grid(axis='y', alpha=0.3)
            ax.axhline(0, color='black', linewidth=0.8)

        plt.savefig(path, dpi=100, bbox_inches='tight')
        plt.close()
    except Exception as e:
        log.warning("_draw_chart_financials failed for %s: %s", ticker_sym, e)
        try:
            plt.close()
        except Exception:
            pass


# ==========================================
# 3. 재무 분석 함수
# ==========================================
async def get_finance_summary(company):
    """
    종목명을 입력받아 DART 재무제표를 조회하고 Gemini로 분석 리포트를 생성합니다.

    Args:
        company (str): 조회할 종목명 (예: "삼성전자")

    Returns:
        str: Gemini가 생성한 재무 분석 텍스트 또는 오류 메시지

    동작 흐름:
        1) 2024년 재무제표 조회 → 없으면 2023년으로 자동 fallback
        2) 매출액·영업이익·당기순이익 행만 필터링
        3) 필터링된 수치를 FINANCE_PROMPT와 함께 Gemini에 전달
        4) 응답 텍스트 반환
    """
    try:
        # 수정 포인트 1: 함수명 fin_stat_all → finstate_all (패키지 업데이트 반영)
        # 수정 포인트 2: 2024년 데이터를 먼저 찾고 없으면 2023년으로 자동 전환
        year = 2024
        df = dart.finstate_all(company, year)
        if df is None or df.empty:
            year = 2023
            df = dart.finstate_all(company, year)

        if df is None or df.empty:
            return f"❌ '{company}'의 재무 데이터를 찾을 수 없습니다. (종목명/상장여부 확인)"

        # 핵심 계정(매출액, 영업이익, 당기순이익)만 필터링해 프롬프트 토큰 절약
        essential = df[df['account_nm'].str.contains('매출액|영업이익|당기순이익', na=False)]
        # 당기(thsstrm) vs 전기(frmtrm) 비교가 가능한 형태로 문자열 변환
        # DART API 컬럼명: thstrm_amount(당기), thstrm_add_amount(당기누적), frmtrm_amount(전기)
        # 어느 컬럼이 실제로 존재하는지 확인 후 사용
        amount_col = next(
            (c for c in ['thstrm_amount', 'thstrm_add_amount'] if c in essential.columns),
            None
        )
        if amount_col is None:
            return f"⚠️ 분석 중 오류가 발생했습니다: 당기금액 컬럼을 찾을 수 없습니다. (컬럼: {list(essential.columns)})"
        cols = ['account_nm', amount_col]
        if 'frmtrm_amount' in essential.columns:
            cols.append('frmtrm_amount')
        data_str = essential[cols].to_string()

        today = date.today().strftime("%Y년 %m월 %d일")
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[FINANCE_PROMPT + f"\n\n작성일: {today}\n종목: {company}\n데이터:\n{data_str}"]
        )
        source_note = f"\n\n📌 출처: DART 전자공시시스템 ({year}년 재무제표)"
        return response.text + source_note
    except Exception as e:
        return f"⚠️ 분석 중 오류가 발생했습니다: {e}"


def fetch_webpage_text(url):
    """
    URL에서 웹페이지 본문 텍스트를 추출합니다.

    Args:
        url (str): 크롤링할 대상 URL

    Returns:
        str | None: 정제된 텍스트(최대 5000자) 또는 실패 시 None

    동작 흐름:
        1) Mozilla User-Agent를 설정해 차단 우회
        2) HTML 태그 제거 후 연속 공백을 단일 공백으로 정규화
        3) 앞 5000자만 반환해 Gemini 입력 토큰 제한에 대응
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore")
        # HTML 태그 제거 → 공백 정규화 → 5000자 제한
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()[:5000]
    except:
        return None

# ==========================================
# 4. 텔레그램 클라이언트 초기화
# ==========================================
# user_client : 일반 사용자 계정으로 채널 메시지를 수신(모니터링)
# bot_client  : 봇 토큰으로 인증해 나에게 메시지를 발송
user_client = TelegramClient("user_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
bot_client = TelegramClient("bot_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)


async def send_weekly_info(mode):
    """
    스케줄러가 호출하는 주간 정보 발송 함수입니다.

    Args:
        mode (str): 'shop' → 장보기 리스트, 그 외 → 나들이 추천

    동작:
        해당 프롬프트로 Gemini 응답을 생성해 내 텔레그램 계정으로 전송합니다.
    """
    prompt = SHOPPING_PROMPT if mode == 'shop' else OUTING_PROMPT
    response = client.models.generate_content(model='gemini-2.5-flash', contents=[prompt])
    await bot_client.send_message(MY_TELEGRAM_ID, response.text)


def _fetch_us_morning_data() -> str:
    """
    미국 시황 데이터를 yfinance로 수집해 텍스트로 반환합니다. (동기 함수 - executor에서 실행)

    수집 항목:
        - 3대 지수: S&P 500, NASDAQ, DOW
        - 국내: KOSPI, KOSDAQ
        - 원자재: 금(GC=F), WTI(CL=F)
        - 금리: 미국 2Y(^IRX), 10Y(^TNX)
        - 변동성: VIX(^VIX)
        - 가상자산: BTC-USD
    """
    TICKERS = {
        'S&P 500':  '^GSPC',
        'NASDAQ':   '^IXIC',
        'DOW':      '^DJI',
        'KOSPI':    '^KS11',
        'KOSDAQ':   '^KQ11',
        '금(Gold)': 'GC=F',
        'WTI':      'CL=F',
        '미국 2Y':  '^IRX',
        '미국 10Y': '^TNX',
        'VIX':      '^VIX',
        'BTC':      'BTC-USD',
    }

    lines = []
    for name, sym in TICKERS.items():
        try:
            hist = yf.Ticker(sym).history(period='2d')
            if hist.empty or len(hist) < 1:
                lines.append(f"{name}: 데이터 없음")
                continue
            cur  = hist['Close'].iloc[-1]
            prev = hist['Close'].iloc[-2] if len(hist) >= 2 else cur
            chg  = cur - prev
            pct  = chg / prev * 100 if prev else 0
            arrow = '▲' if chg >= 0 else '▼'
            sign  = '+' if chg >= 0 else ''

            # 금리/VIX는 소수점 2자리, 가격은 콤마 포맷
            if sym in ('^IRX', '^TNX', '^VIX'):
                lines.append(f"{name}: {cur:.2f}  {arrow} {sign}{pct:.1f}%")
            elif sym == 'BTC-USD':
                lines.append(f"{name}: ${cur:,.0f}  {arrow} {sign}{pct:.1f}%")
            elif sym in ('GC=F', 'CL=F'):
                lines.append(f"{name}: ${cur:,.1f}  {arrow} {sign}{pct:.1f}%")
            elif sym in ('^KS11', '^KQ11'):
                lines.append(f"{name}: {cur:,.2f}  {arrow} {sign}{pct:.1f}%")
            else:
                lines.append(f"{name}: {cur:,.2f}  {arrow} {sign}{pct:.1f}%")
        except Exception as e:
            lines.append(f"{name}: 조회 실패 ({e})")

    return '\n'.join(lines)


async def send_us_morning():
    """
    매일 07:00 KST 자동 실행되는 미국 시황 브리핑 함수입니다.

    동작 흐름:
        1) yfinance로 주요 지수/원자재/금리/BTC 전일 종가 수집
        2) 수집 데이터를 Gemini에 전달해 시황 요약 생성
        3) 텔레그램으로 전송
    """
    loop = asyncio.get_event_loop()
    data_str = await loop.run_in_executor(_executor, _fetch_us_morning_data)

    today = date.today().strftime('%Y년 %m월 %d일')
    prompt = (
        f"당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 수석 애널리스트입니다.\n"
        f"아래는 {today} 기준 글로벌 시장 주요 지표입니다.\n\n"
        f"{data_str}\n\n"
        f"아래 형식으로 간결하게 정리하세요.\n\n"
        f"[🌅 모닝 시황 브리핑 - {today}]\n\n"
        f"■ 미국 3대 지수 흐름\n"
        f"- S&P500 / NASDAQ / DOW 등락 요약 및 전일 대비 의미\n\n"
        f"■ 국내 시장 전망\n"
        f"- KOSPI / KOSDAQ 오늘 시가 방향 예상 (미국 흐름 반영)\n\n"
        f"■ 원자재 & 금리\n"
        f"- 금, WTI, 미국 2Y/10Y 금리 동향 및 투자 시사점\n\n"
        f"■ 리스크 & 기회\n"
        f"- 오늘 주목할 매크로 변수 또는 이벤트 1~2가지\n\n"
        f"3줄 이내로 각 섹션을 간결하게 서술하세요."
    )

    response = await loop.run_in_executor(
        _executor,
        lambda: client.models.generate_content(model='gemini-2.5-flash', contents=[prompt])
    )

    raw_data_block = f"\n\n📌 **원본 데이터**\n```\n{data_str}\n```"
    await bot_client.send_message(
        MY_TELEGRAM_ID,
        response.text + raw_data_block
    )


# ==========================================
# 5. 이벤트 핸들러
# ==========================================

def extract_youtube_id(url):
    """유튜브 URL에서 영상 ID를 추출합니다."""
    match = re.search(r'(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})', url)
    return match.group(1) if match else None


# ==========================================
# 5-1. 관심종목 DART 공시 자동 감지 (스케줄 작업)
# ==========================================
async def check_dart_watchlist():
    """
    관심종목(watchlist.json)의 DART 공시를 주기적으로 확인합니다.

    동작 흐름:
        1) watchlist.json에서 관심종목 목록을 로드
        2) 오늘 날짜 기준으로 각 종목의 최신 공시를 DART API로 조회
        3) 이미 알림을 보낸 공시(seen_filings.json)는 건너뜀
        4) 새 공시 발견 시:
           - DART_ALERT_KEYWORDS 포함 여부에 따라 🚨(중요) / 📢(일반) 구분
           - 공시 링크와 함께 텔레그램으로 알림 전송
        5) 수신 이력 업데이트
    """
    stocks = load_watchlist()
    if not stocks:
        return

    seen = load_seen_filings()
    today = date.today().strftime('%Y-%m-%d')
    loop = asyncio.get_event_loop()
    new_seen = set(seen)

    for comp in stocks:
        try:
            reports = await loop.run_in_executor(_executor, dart.list, comp, today)
            if reports is None or reports.empty:
                continue
            for _, row in reports.iterrows():
                rcpNo = str(row['rcept_no'])
                if rcpNo in seen:
                    continue
                report_nm = str(row['report_nm'])
                is_alert = any(kw in report_nm for kw in DART_ALERT_KEYWORDS)
                flag = "🚨" if is_alert else "📢"
                link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcpNo}"
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"{flag} **[관심종목 공시]** {comp}\n"
                    f"▪️ {row['rcept_dt']} | [{report_nm}]({link})",
                    link_preview=False,
                )
                new_seen.add(rcpNo)
        except Exception as e:
            log.warning("DART watchlist check failed for %s: %s", comp, e)

    if new_seen != seen:
        save_seen_filings(new_seen)


async def on_channel_msg(event):
    """
    WATCH_CHANNELS 에 등록된 채널에 새 메시지가 올라올 때 자동 실행됩니다.

    동작 흐름:
        [유튜브] YouTube URL 감지 → 자막 추출 → Gemini 요약 → 원문 링크 첨부
        [기사/블로그] 일반 URL 감지 → 웹 크롤링 → Gemini 요약 → 원문 링크 첨부
        [PDF] 문서 첨부 감지 → 파일 다운로드 → Gemini Files API 업로드 → 리포트 분석 → 파일 삭제
    """
    chat = await event.get_chat()
    source_name = chat.title if hasattr(chat, 'title') else "정보 채널"
    log.info("채널 메시지 수신: %s | 내용 앞 50자: %s", source_name, (event.text or "")[:50])

    urls = re.findall(r'https?://[^\s]+', event.text or "")

    # ── 유튜브 ──────────────────────────────────────────
    if urls and re.search(r'(youtube\.com/watch|youtu\.be/)', urls[0]):
        yt_url = urls[0]
        vid_id = extract_youtube_id(yt_url)
        if vid_id:
            try:
                segments = YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
                transcript = ' '.join(s['text'] for s in segments)[:5000]
                # NOTE: generate_content()는 동기 호출 → 이벤트 루프 블로킹 발생 가능
                # 고부하 환경에서는 run_in_executor 로 오프로드 권장
                res = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[LINK_PROMPT + f"\n내용: {transcript}"]
                )
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"▶️ **[유튜브 인사이트 요약]**\n📡 채널: {source_name}\n\n{res.text}\n\n📎 원문: {yt_url}"
                )
            except Exception as e:
                await bot_client.send_message(
                    MY_TELEGRAM_ID,
                    f"▶️ **[유튜브]** 자막을 가져올 수 없습니다 ({e})\n📡 채널: {source_name}\n📎 원문: {yt_url}"
                )
        return

    # ── 기사 / 블로그 ────────────────────────────────────
    # NOTE: fetch_webpage_text()는 동기 urllib 호출 (네트워크 블로킹)
    # NOTE: generate_content()도 동기 호출 → 두 번 연속으로 이벤트 루프를 블로킹합니다.
    # 채널 메시지가 빈번할 경우 CPU/응답 지연의 주원인이 됩니다.
    if urls:
        text = fetch_webpage_text(urls[0])
        if text:
            res = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[LINK_PROMPT + f"\n내용: {text}"]
            )
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"🌐 **[네모 봇 인사이트 요약]**\n📡 채널: {source_name}\n\n{res.text}\n\n📎 원문: {urls[0]}"
            )
        return

    # ── PDF (문서 첨부) ──────────────────────────────────
    if event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        file_path = await event.download_media(file=DOWNLOAD_DIR)
        caption = (event.text or '').strip() or '(제목 없음)'
        filename = os.path.basename(file_path) if file_path else '다운로드 실패'

        if not file_path:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신 실패]**\n{caption}\n\n⚠️ 파일 다운로드에 실패했습니다."
            )
            return

        await bot_client.send_message(
            MY_TELEGRAM_ID,
            f"📄 **[PDF 분석 중...]**\n📡 채널: {source_name}\n{caption}\n\n⏳ Gemini가 리포트를 읽고 있습니다..."
        )
        try:
            # Gemini Files API에 PDF 업로드 → 멀티모달 분석
            uploaded = client.files.upload(file=file_path)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[uploaded, PDF_PROMPT]
            )
            # 분석 완료 후 Gemini 서버에서 파일 삭제
            client.files.delete(name=uploaded.name)
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📊 **[PDF 리포트 분석]**\n📡 채널: {source_name}\n_{caption}_\n\n{response.text}\n\n📎 파일명: {filename}"
            )
        except Exception as e:
            await bot_client.send_message(
                MY_TELEGRAM_ID,
                f"📄 **[PDF 수신]**\n📡 채널: {source_name}\n{caption}\n\n⚠️ 분석 중 오류 발생: {e}\n📎 파일명: {filename}"
            )


# NOTE: 핸들러 등록은 user_client.start() 이후 main() 안에서 수행됩니다.


@bot_client.on(events.NewMessage())
async def on_bot_msg(event):
    """
    봇에게 전달되는 모든 메시지를 처리합니다.
    보안을 위해 MY_TELEGRAM_ID 에서 보낸 메시지만 응답합니다.

    지원 명령어:
        /재무 <종목명>  → DART 재무 분석 리포트 + 실시간 주가 + 차트
        /공시 <종목명>  → 최근 주요 공시 3건 링크
        /us <종목명>    → 미국 종목 실시간 주가·밸류에이션 + 차트 (한글명/ticker 모두 가능)
        /watch <종목명> → DART 공시 관심종목 추가
        /unwatch <종목명> → 관심종목 삭제
        /watchlist      → 관심종목 목록 조회
        /keywords       → DART 공시 알림 키워드 목록 확인
        /시황           → 미국 시황 브리핑 즉시 요청 (매일 07:00 자동 발송)
        /장보기         → 주간 장보기 리스트 즉시 요청
        /나들이         → 나들이 장소 즉시 요청
        /help (/도움말) → 전체 명령어 도움말 출력
    """
    # 내 계정에서 보낸 메시지만 처리 (무단 접근 차단)
    if event.chat_id != MY_TELEGRAM_ID:
        return
    text = event.text

    if text == '/시황':
        await event.reply("🌅 미국 시황 데이터 수집 중...")
        await send_us_morning()

    elif text.startswith('/재무'):
        comp = text.replace('/재무', '').strip()
        await event.reply(f"📊 **{comp}** 분석 중... (주가 조회 + DART 재무 + 차트 생성)")
        loop = asyncio.get_event_loop()

        # DART 재무 분석 + yfinance 주가 병렬 처리
        ticker = get_kr_ticker(comp)
        fin_task   = asyncio.ensure_future(get_finance_summary(comp))
        price_task = loop.run_in_executor(_executor, get_price_info_kr, ticker, comp) if ticker else None

        fin_text   = await fin_task
        price_text = (await price_task) if price_task else ''

        # 차트 생성
        charts = []
        if ticker:
            path_d  = os.path.join(CHARTS_DIR, f"{comp}_daily.png")
            path_w  = os.path.join(CHARTS_DIR, f"{comp}_weekly.png")
            path_fin = os.path.join(CHARTS_DIR, f"{comp}_financials.png")
            await loop.run_in_executor(_executor, _draw_chart_kr, ticker, comp, path_d, path_w)
            await loop.run_in_executor(_executor, _draw_chart_financials, ticker, comp, path_fin)
            charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]

        # 텍스트 전송
        header = f"📈 **[{comp} 투자 분석 리포트]**\n\n"
        if price_text:
            header += price_text + "\n\n━━━━━━━━━━━━━━━━━━━━\n\n"
        full = header + fin_text
        for i in range(0, len(full), 4096):
            await event.reply(full[i:i+4096])

        # 차트 이미지 전송 후 파일 삭제
        if charts:
            await bot_client.send_file(MY_TELEGRAM_ID, charts)
            for p in charts:
                try:
                    os.remove(p)
                except Exception:
                    pass

    elif text == '/장보기':
        await send_weekly_info('shop')
    elif text == '/나들이':
        await send_weekly_info('out')

    elif text.startswith('/us ') or text.lower().startswith('/us '):
        query = text[4:].strip()
        await event.reply(f"🇺🇸 **{query}** 미국 종목 조회 중...")
        loop = asyncio.get_event_loop()

        # 리포트 텍스트 + 주가 차트 병렬 생성
        ticker = US_STOCK_MAP.get(query.lower(), US_STOCK_MAP.get(query, query.upper()))
        report_task = loop.run_in_executor(_executor, get_us_report_text, query)

        path_d   = os.path.join(CHARTS_DIR, f"US_{ticker}_daily.png")
        path_w   = os.path.join(CHARTS_DIR, f"US_{ticker}_weekly.png")
        path_fin = os.path.join(CHARTS_DIR, f"US_{ticker}_financials.png")
        chart_task = loop.run_in_executor(_executor, _draw_chart_us, query, path_d, path_w)

        report_text = await report_task
        await chart_task

        # 분기 실적 차트 (주가 차트 완료 후 순차 실행)
        await loop.run_in_executor(_executor, _draw_chart_financials, ticker, query, path_fin)

        full = f"📊 **[미국 종목 리포트]**\n\n{report_text}"
        for i in range(0, len(full), 4096):
            await event.reply(full[i:i+4096])

        charts = [p for p in [path_d, path_w, path_fin] if os.path.exists(p)]
        if charts:
            await bot_client.send_file(MY_TELEGRAM_ID, charts)
            for p in charts:
                try:
                    os.remove(p)
                except Exception:
                    pass

    elif text.startswith('/공시'):
        comp = text.replace('/공시', '').strip()
        # 수정 포인트 3: 검색 시작일을 2025년으로 업데이트하여 최신성 확보
        reports = dart.list(comp, start='2025-01-01')
        if reports is None or reports.empty:
            return await event.reply(f"❌ '{comp}'의 최근 공시가 없습니다.")

        msg = f"📑 **[{comp} 최근 주요 공시]**\n\n"
        for _, row in reports.head(3).iterrows():
            # DART 공시 상세 링크를 접수번호(rcpNo)로 구성
            msg += f"▪️ {row['rcept_dt']} | [{row['report_nm']}](https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']})\n"
        await event.reply(msg, link_preview=False)

    # ── 관심종목 관리 ──────────────────────────────────────
    elif text.startswith('/watch ') and not text.startswith('/watchlist'):
        name = text[7:].strip()
        if not name:
            return await event.reply("사용법: /watch 종목명")
        ok, msg = add_to_watchlist(name)
        await event.reply(msg)

    elif text.startswith('/unwatch'):
        name = text.replace('/unwatch', '').strip()
        if not name:
            return await event.reply("사용법: /unwatch 종목명")
        ok, msg = remove_from_watchlist(name)
        await event.reply(msg)

    elif text == '/watchlist':
        stocks = load_watchlist()
        if not stocks:
            return await event.reply("📋 관심종목이 비어 있습니다.\n\n/watch 삼성전자  →  추가")
        msg = f"📋 **관심종목 ({len(stocks)}종목)**\n\n"
        for i, s in enumerate(stocks, 1):
            msg += f"{i}. {s}\n"
        msg += "\n/watch 종목명  →  추가\n/unwatch 종목명  →  삭제"
        await event.reply(msg)

    elif text == '/keywords':
        msg = "🔑 **DART 공시 알림 키워드**\n\n"
        for kw in DART_ALERT_KEYWORDS:
            msg += f"• {kw}\n"
        msg += f"\n총 {len(DART_ALERT_KEYWORDS)}개 키워드"
        await event.reply(msg)

    # ── 모니터링 채널 관리 ─────────────────────────────────
    elif text.startswith('/채널추가'):
        ch = text.replace('/채널추가', '').strip()
        if not ch:
            return await event.reply("사용법: /채널추가 @채널유저네임")
        if not ch.startswith('@'):
            ch = '@' + ch
        channels = load_channels()
        if ch in channels:
            return await event.reply(f"'{ch}'은(는) 이미 모니터링 중입니다.")
        channels.append(ch)
        save_channels(channels)
        await update_channel_handler(channels)
        await event.reply(f"✅ '{ch}' 모니터링 채널에 추가됐습니다. (총 {len(channels)}개)\n\n⚠️ user_client 계정이 해당 채널에 가입되어 있어야 합니다.")

    elif text.startswith('/채널삭제'):
        ch = text.replace('/채널삭제', '').strip()
        if not ch:
            return await event.reply("사용법: /채널삭제 @채널유저네임")
        if not ch.startswith('@'):
            ch = '@' + ch
        channels = load_channels()
        if ch not in channels:
            return await event.reply(f"'{ch}'은(는) 모니터링 목록에 없습니다.")
        channels.remove(ch)
        save_channels(channels)
        await update_channel_handler(channels)
        await event.reply(f"🗑️ '{ch}' 모니터링 채널에서 삭제됐습니다. (총 {len(channels)}개)")

    elif text == '/채널목록':
        channels = load_channels()
        if not channels:
            return await event.reply("📡 모니터링 채널이 없습니다.\n\n/채널추가 @채널유저네임  →  추가")
        msg = f"📡 **모니터링 채널 ({len(channels)}개)**\n\n"
        for i, ch in enumerate(channels, 1):
            msg += f"{i}. {ch}\n"
        msg += "\n/채널추가 @유저네임  →  추가\n/채널삭제 @유저네임  →  삭제"
        await event.reply(msg)

    elif text in ('/help', '/도움말'):
        msg = (
            "📖 **네모 봇 명령어 도움말**\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📊 **주식 조회**\n"
            "  `/재무 <종목명>` — 국내 종목 DART 재무 분석 + 주가 + 차트\n"
            "    └ 일봉(3M) · 주봉(5Y) · 분기 실적(TTM) 차트 자동 첨부\n"
            "  `/공시 <종목명>` — 국내 종목 최근 주요 공시 3건\n"
            "  `/us <종목명/티커>` — 미국 종목 주가·밸류에이션 + 차트\n"
            "    └ 일봉(3M) · 주봉(5Y) · 분기 실적(TTM) 차트 자동 첨부\n"
            "    예) `/us NVDA`, `/us 엔비디아`\n\n"
            "👀 **관심종목 & DART 알림**\n"
            "  `/watch <종목명>` — 관심종목 추가\n"
            "  `/unwatch <종목명>` — 관심종목 삭제\n"
            "  `/watchlist` — 관심종목 전체 목록\n"
            "  `/keywords` — DART 공시 알림 키워드 확인\n"
            "    ※ 평일 09:00~18:00 매 30분 자동 감지\n\n"
            "📡 **채널 모니터링 관리**\n"
            "  `/채널추가 @유저네임` — 모니터링 채널 추가\n"
            "  `/채널삭제 @유저네임` — 모니터링 채널 삭제\n"
            "  `/채널목록` — 현재 모니터링 중인 채널 목록\n\n"
            "📈 **시황 브리핑**\n"
            "  `/시황` — 미국 3대 지수·원자재·금리·BTC 시황 즉시 조회\n"
            "    ※ 매일 07:00 자동 발송\n\n"
            "🏠 **생활 브리핑**\n"
            "  `/장보기` — 주간 건강 식단 + 장보기 리스트\n"
            "  `/나들이` — 아이와 가기 좋은 나들이 장소 추천\n"
            "    ※ 금 09:00 장보기 / 목 18:00 나들이 자동 전송\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🤖 **자동 기능**\n"
            "  • 모니터링 채널에 링크 포함 메시지 → AI 핵심 요약\n"
            "  • YouTube 링크 → 자막 기반 요약\n"
            "  • PDF 첨부 → 주식 리포트 자동 분석\n\n"
            "📨 **봇에게 직접 보내기**\n"
            "  • 기사/블로그 링크를 그냥 붙여넣기 → AI 요약\n"
            "  • YouTube 링크 → 자막 기반 요약\n"
            "  • PDF 파일 첨부 → 리포트 분석"
        )
        await event.reply(msg)

    # ── 봇 채팅창: PDF 첨부 직접 분석 ────────────────────────────────
    elif event.document and getattr(event.document, 'mime_type', '') == 'application/pdf':
        file_path = await event.download_media(file=DOWNLOAD_DIR)
        caption = (text or '').strip() or '(제목 없음)'
        filename = os.path.basename(file_path) if file_path else '다운로드 실패'

        if not file_path:
            await event.reply("⚠️ PDF 다운로드에 실패했습니다.")
            return

        await event.reply("📄 **PDF 분석 중...**\n⏳ Gemini가 리포트를 읽고 있습니다...")
        try:
            uploaded = client.files.upload(file=file_path)
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[uploaded, PDF_PROMPT]
            )
            client.files.delete(name=uploaded.name)
            await event.reply(
                f"📊 **[PDF 리포트 분석]**\n_{caption}_\n\n{response.text}\n\n📎 파일명: {filename}"
            )
        except Exception as e:
            await event.reply(f"⚠️ PDF 분석 중 오류 발생: {e}")

    # ── 봇 채팅창: URL 직접 요약 (명령어가 아닌 링크 메시지) ──────────
    elif text and not text.startswith('/'):
        urls = re.findall(r'https?://[^\s]+', text)
        if not urls:
            return

        url = urls[0]
        # 유튜브
        if re.search(r'(youtube\.com/watch|youtu\.be/)', url):
            vid_id = extract_youtube_id(url)
            if not vid_id:
                return await event.reply("⚠️ YouTube 영상 ID를 인식할 수 없습니다.")
            await event.reply("▶️ **유튜브 자막 추출 중...**")
            try:
                segments = YouTubeTranscriptApi.get_transcript(vid_id, languages=['ko', 'en'])
                transcript = ' '.join(s['text'] for s in segments)[:5000]
                loop = asyncio.get_event_loop()
                res = await loop.run_in_executor(
                    _executor,
                    lambda: client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=[LINK_PROMPT + f"\n내용: {transcript}"]
                    )
                )
                await event.reply(
                    f"▶️ **[유튜브 인사이트 요약]**\n\n{res.text}\n\n📎 원문: {url}"
                )
            except Exception as e:
                await event.reply(f"⚠️ 자막을 가져올 수 없습니다: {e}\n📎 원문: {url}")
        else:
            # 일반 기사/블로그
            await event.reply("🌐 **링크 분석 중...**")
            loop = asyncio.get_event_loop()
            page_text = await loop.run_in_executor(_executor, fetch_webpage_text, url)
            if not page_text:
                return await event.reply(f"⚠️ 해당 링크에서 내용을 가져오지 못했습니다.\n📎 {url}")
            res = await loop.run_in_executor(
                _executor,
                lambda: client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[LINK_PROMPT + f"\n내용: {page_text}"]
                )
            )
            await event.reply(
                f"🌐 **[네모 봇 인사이트 요약]**\n\n{res.text}\n\n📎 원문: {url}"
            )

# ==========================================
# 6. 메인 가동 루틴
# ==========================================
async def main():
    """
    봇의 진입점(entry point)입니다.

    동작 순서:
        1) user_client (일반 계정) 로그인 → 채널 모니터링 시작
        2) bot_client (봇 토큰) 로그인  → 명령어 수신 시작
        3) APScheduler 등록
           - 금요일 09:00 KST: 장보기 리스트 전송
           - 목요일 18:00 KST: 나들이 추천 전송
        4) 시작 알림 메시지 발송
        5) 두 클라이언트를 asyncio.gather 로 동시에 유지
    """
    # SQLite session 락: 이전 프로세스가 완전히 종료되기 전에 접근하면
    # "database is locked" 발생 → 최대 5회, 2초 간격으로 재시도
    for attempt in range(1, 6):
        try:
            await user_client.start()
            await bot_client.start(bot_token=TELEGRAM_BOT_TOKEN)
            break
        except Exception as exc:  # sqlite3.OperationalError 포함
            if attempt == 5:
                raise
            log.warning("클라이언트 시작 실패 (%d/5): %s — 2초 후 재시도", attempt, exc)
            await asyncio.sleep(2)

    # 클라이언트 연결 완료 후 채널 핸들러 등록
    # channels.json 우선, 없으면 WATCH_CHANNELS 환경변수 사용
    _init_channels = load_channels()
    if _init_channels:
        user_client.add_event_handler(on_channel_msg, events.NewMessage(chats=_init_channels))
        log.info("채널 모니터링 등록 완료: %s", _init_channels)
    else:
        log.info("모니터링 채널 미설정 — /채널추가 @유저네임 으로 추가하세요")

    # 스케줄러 설정 (목/금 정기 브리핑 + 모닝 시황 + 관심종목 DART 공시 감지)
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='fri', hour=9, args=['shop'])
    scheduler.add_job(send_weekly_info, 'cron', day_of_week='thu', hour=18, args=['out'])
    # 매일 07:00 미국 시황 브리핑 (주말 포함 — 미국 선물/BTC는 주말도 움직임)
    scheduler.add_job(send_us_morning, 'cron', hour=7, minute=0)
    # 관심종목 DART 공시 30분마다 감지 (장 시간대 집중: 평일 09:00~18:00)
    scheduler.add_job(
        check_dart_watchlist,
        'cron',
        day_of_week='mon-fri',
        hour='9-18',
        minute='*/30',
    )
    scheduler.start()

    # 봇 가동 완료 알림 + 사용 가능한 명령어 안내
    await bot_client.send_message(MY_TELEGRAM_ID,
        "🚀 **네모 봇 올인원 엔진 정상 가동!**\n\n"
        "🔹 국내: /재무 삼성전자, /공시 삼성전자\n"
        "🔹 미국: /us NVDA, /us 엔비디아\n"
        "🔹 관심종목: /watch 종목명, /unwatch 종목명, /watchlist\n"
        "🔹 키워드: /keywords (DART 공시 알림 키워드 확인)\n"
        "🔹 시황: /시황 (매일 07:00 자동 발송)\n"
        "🔹 생활: /장보기, /나들이\n"
        "🔹 자동: 외부 채널 링크 자동 요약 가동 중\n\n"
        "📖 전체 명령어 보기: /help"
    )

    # 두 클라이언트를 동시에 실행하여 채널 수신과 봇 명령 처리를 병행
    # ⚠️ [이벤트 루프 블로킹 주의]
    # on_channel_msg / on_bot_msg 핸들러 내 Gemini API 호출은 동기 함수입니다.
    # 해당 호출이 진행되는 동안 asyncio 이벤트 루프가 멈추며 CPU 사용률이 급등합니다.
    # 증상: ps aux 에서 main.py 프로세스가 ~100% CPU 점유
    # 해결: loop.run_in_executor(_executor, lambda: client.models.generate_content(...)) 로 변경
    await asyncio.gather(user_client.run_until_disconnected(), bot_client.run_until_disconnected())

if __name__ == "__main__":
    asyncio.run(main())
