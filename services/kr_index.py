"""국내 지수(코스피/코스닥) 보조 조회 — 네이버 금융 모바일 API.

아침 시황(07시 장 시작 전)엔 yfinance가 '어제 종가' 일봉을 아직 안 올려, 봇이 옛 수치를
'전일'로 둔갑시키지 않으려고 "전일 데이터 지연"으로 막는 경우가 잦다(메모: yfinance 일봉 갭).
네이버는 장 마감 직후 전일 종가와 등락률을 직접 제공하므로 yfinance가 막힐 때 폴백으로 쓴다.
등락률을 네이버 서버가 계산해 주므로(두 일봉을 직접 빼지 않음) yfinance 갭으로 인한
'다중일 변동 둔갑' 문제 자체가 생기지 않는다.

stdlib urllib만 사용(memory_spot.py·DATAGO 패턴) — 의존성 추가 없음.
신뢰된 고정 엔드포인트라 SSRF 가드 불필요. 어떤 실패든 None 반환(조용히 — 폴백 실패 시
호출부가 기존 '전일 데이터 지연' 안내로 되돌아간다).
"""
import json
import logging
import urllib.request
from datetime import date, datetime

log = logging.getLogger(__name__)

# 네이버 금융 모바일 지수 API — closePrice/fluctuationsRatio/localTradedAt 등 JSON 반환
_NAVER_INDEX_URL = "https://m.stock.naver.com/api/index/{code}/basic"

# yfinance 심볼 → 네이버 지수 코드
_YF_TO_NAVER = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ"}


def _to_float(v) -> float | None:
    """'8,964.10' 같은 콤마 포함 문자열/숫자를 float로. 실패 시 None."""
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def fetch_naver_kr_index(yf_symbol: str) -> dict | None:
    """yfinance 심볼(^KS11/^KQ11)로 네이버 지수 종가·등락률을 조회.

    반환: {"price": float, "pct": float, "change": float, "date": date|None}
          또는 실패 시 None.
    price/pct/change는 네이버가 계산한 '직전 종가 대비' 값이다(장 시작 전이면 전일 마감 기준).
    date는 데이터 기준 거래일(localTradedAt) — 표시 투명성·신선도 확인용.
    """
    code = _YF_TO_NAVER.get(yf_symbol)
    if code is None:
        return None

    url = _NAVER_INDEX_URL.format(code=code)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        log.warning("네이버 지수(%s) 조회 실패: %s", code, e)
        return None

    price = _to_float(data.get("closePrice"))
    pct = _to_float(data.get("fluctuationsRatio"))
    change = _to_float(data.get("compareToPreviousClosePrice"))
    if price is None or price <= 0 or pct is None:
        log.warning("네이버 지수(%s) 응답 형식 이상 — price=%r pct=%r", code,
                    data.get("closePrice"), data.get("fluctuationsRatio"))
        return None

    traded_date: date | None = None
    traded = data.get("localTradedAt")  # 예: "2026-06-25T15:25:00+09:00"
    if traded:
        try:
            traded_date = datetime.fromisoformat(traded).date()
        except (TypeError, ValueError):
            traded_date = None

    return {"price": price, "pct": pct, "change": change, "date": traded_date}
