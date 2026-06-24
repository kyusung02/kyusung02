"""
반도체 업황 스크리닝 데이터 수집.

영상(2026-05-16, '하이닉스 투자자라면…')의 프레임 — PER vs PBR 평가, LTA 도입으로
시클리컬→안정적 이익 전환 가설, AI 수요·빅테크 CAPEX 추적 — 을 yfinance 기반으로 옮긴다.

데이터 한계 메모:
- DRAM/NAND 현물가는 TrendForce 유료라 직접 못 가져온다.
  대신 (a) 메모리 ETF SMH/SOXX/^SOX 가격, (b) 빅테크 4사 주가, (c) 메모리 직접 노출
  종목(005930.KS, 000660.KS, MU)의 가격 동향으로 프록시.
- 빅테크 CAPEX/RPO 실 숫자는 분기 1회 수동 업데이트가 맞으므로 여기선 가격만.
- 한국 DRAM 수출 단가(관세청)는 data.go.kr API 키 발급 후 Phase 2.
"""
import os
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from urllib import request, parse
import yfinance as yf
from utils import kst_now

log = logging.getLogger(__name__)


# ── 종목 군집 (영상에서 다룬 분류 + 한국 시장 노출) ───────────────────────────

# 한국 메모리 본진. KR_SECTORS["반도체"]와 부분 중복이지만 명시적으로 가져간다.
KR_MEMORY = [
    ("삼성전자",     "005930.KS"),
    ("SK하이닉스",   "000660.KS"),
]

# 한국 후공정·장비·소재·테스트 (메모리 빅사이클의 수혜주)
KR_EQUIP = [
    ("한미반도체",   "042700.KS"),
    ("리노공업",     "058470.KS"),
    ("이오테크닉스", "039030.KS"),
    ("원익IPS",      "240810.KS"),
    ("하나마이크론", "067310.KS"),
    ("피에스케이",   "319660.KQ"),
    ("네오셈",       "253590.KQ"),
]

# 미국 메모리 직접 노출 (DRAM/NAND) — 영상의 메모리 본진 피어
US_MEMORY_PEERS = [
    ("Micron",   "MU"),      # DRAM + NAND
    ("SanDisk",  "SNDK"),    # 2025-02 WDC 분사, NAND 순수 플레이
    ("Kioxia",   "285A.T"),  # 일본 NAND, 2024-12 도쿄 상장 (데이터 빈약 가능)
]

# 미국 팹리스·파운드리 — TSMC는 PER 비교 기준점, NVIDIA는 HBM 최대 수요처
US_FABLESS = [
    ("TSMC",     "TSM"),
    ("NVIDIA",   "NVDA"),
    ("Broadcom", "AVGO"),
    ("AMD",      "AMD"),
    ("ARM",      "ARM"),
]

# 스토리지·HDD — 영상에서 "메모리와 분리"로 분류됐지만 NAND 대체재로 추적
US_STORAGE = [
    ("Western Digital", "WDC"),
]

# 반도체 장비 (자본 지출 사이클)
US_EQUIP = [
    ("ASML",          "ASML"),
    ("Applied Mat.",  "AMAT"),
    ("Lam Research",  "LRCX"),
    ("KLA",           "KLAC"),
]

# 빅테크 4사 — 영상에서 'CAPEX 4사 합계 $680B' 언급한 곳들
BIGTECH_CAPEX = [
    ("Microsoft", "MSFT"),
    ("Alphabet",  "GOOGL"),
    ("Meta",      "META"),
    ("Amazon",    "AMZN"),
]

# 지수·ETF (시장 전체 모멘텀)
INDICES_ETFS = {
    "필라델피아 SOX":   "^SOX",
    "반도체 ETF (SMH)": "SMH",
    "반도체 ETF (SOXX)": "SOXX",
    "한국 KOSPI":       "^KS11",
    "한국 KOSDAQ":      "^KQ11",
}


def _build_all_tickers():
    """수집 대상 (이름, 티커, 그룹) 튜플 리스트."""
    rows = []
    for name, tk in KR_MEMORY:        rows.append((name, tk, "KR 메모리"))
    for name, tk in KR_EQUIP:         rows.append((name, tk, "KR 장비/소재"))
    for name, tk in US_MEMORY_PEERS:  rows.append((name, tk, "US 메모리 (DRAM/NAND)"))
    for name, tk in US_FABLESS:       rows.append((name, tk, "US 팹리스·파운드리"))
    for name, tk in US_STORAGE:       rows.append((name, tk, "US 스토리지/HDD"))
    for name, tk in US_EQUIP:         rows.append((name, tk, "US 장비"))
    for name, tk in BIGTECH_CAPEX:    rows.append((name, tk, "빅테크 CAPEX"))
    return rows


def _safe_close_series(raw, ticker: str, multi: bool):
    """yfinance 배치 응답에서 종가 시리즈를 안전 추출. 데이터 없으면 None."""
    try:
        if multi:
            lvl0 = raw.columns.get_level_values(0)
            if ticker not in lvl0:
                return None
            s = raw[ticker]["Close"].dropna()
        else:
            s = raw["Close"].dropna()
        return s if len(s) >= 2 else None
    except Exception:
        return None


def _bdays_strictly_between(d0, d1) -> int:
    """d0, d1(date) 사이에 끼인 영업일(주중) 수. 두 일봉이 인접 거래일인지 판별용."""
    n = 0
    d = d0 + timedelta(days=1)
    while d < d1:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _pct_change(s) -> float | None:
    if s is None or len(s) < 2:
        return None
    # 직전 두 일봉이 영업일 기준 인접하지 않으면(=중간 거래일 바 누락) 전일 등락이
    # 다중일 변동률로 둔갑하므로(섹터 HPSP -24% 사례) 산출하지 않고 생략한다.
    # 시장(KR/US/JP)이 섞여 있어 단일 거래일 달력을 못 쓰므로 시장 무관 영업일
    # 인접성으로 판별. (장전 08시라 previous_close 보정은 0%가 되어 못 씀.)
    if _bdays_strictly_between(s.index[-2].date(), s.index[-1].date()) > 0:
        return None
    cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
    if prev == 0:
        return None
    return (cur - prev) / prev * 100


def _change_over(s, n: int) -> float | None:
    """현재 종가 대비 n 거래일 전 대비 변동률."""
    if s is None or len(s) <= n:
        return None
    cur = float(s.iloc[-1])
    past = float(s.iloc[-n - 1])
    if past == 0:
        return None
    return (cur - past) / past * 100


# ── 관세청 메모리 수출단가 (data.go.kr 수출입무역통계, Phase 2) ────────────────
# DRAM/NAND 월별 수출액·수출단가($/kg)의 MoM/YoY = 주가 프록시로는 못 보는 공식
# 펀더멘털 신호. DATAGO_API_KEY는 VM .env에서 os.environ으로 직접 판독(config.py
# 동기화 불필요 — HEALTHCHECK_URL과 동일 패턴). 미설정 시 무동작이라 안전.
_DATAGO_KEY    = os.environ.get("DATAGO_API_KEY", "").strip()
_ITEMTRADE_URL = "https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList"
_MEM_HS        = {"8542321010": "DRAM", "8542321030": "NAND"}   # 디램 / 플래시 메모리


def _yymm_shift(yymm: int, delta_months: int) -> int:
    """YYYYMM 정수에 개월수 가감. 예: (202601, -2) -> 202511."""
    y, m = divmod(yymm, 100)
    idx = y * 12 + (m - 1) + delta_months
    return (idx // 12) * 100 + (idx % 12) + 1


def _yymm_to_dot(yymm: int) -> str:
    """202505 -> '2025.05' (API year 필드 포맷)."""
    return f"{yymm // 100}.{yymm % 100:02d}"


def _itemtrade(strt: int, end: int) -> list:
    """관세청 품목별 수출입실적 조회(HS 854232 메모리, strt~end 월). 실패 시 []."""
    qs = parse.urlencode({
        "serviceKey": _DATAGO_KEY, "strtYymm": strt, "endYymm": end, "hsSgn": "854232",
    })
    try:
        with request.urlopen(f"{_ITEMTRADE_URL}?{qs}", timeout=20) as resp:
            root = ET.fromstring(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("관세청 API 호출/파싱 실패(%s~%s): %s", strt, end, e)
        return []
    if root.findtext("./header/resultCode") != "00":
        log.warning("관세청 API 비정상: %s / %s",
                    root.findtext("./header/resultCode"),
                    root.findtext("./header/resultMsg"))
        return []
    rows = []
    for it in root.findall("./body/items/item"):
        hs = (it.findtext("hsCode") or "").strip()
        if hs not in _MEM_HS:        # 총계(hsCode '-')·기타 품목 제외
            continue
        try:
            rows.append({
                "hs":      hs,
                "month":   (it.findtext("year") or "").strip(),   # "2026.05"
                "exp_dlr": float(it.findtext("expDlr") or 0),
                "exp_wgt": float(it.findtext("expWgt") or 0),
            })
        except ValueError:
            continue
    return rows


def fetch_kr_memory_export() -> dict:
    """한국 DRAM/NAND 월별 수출액·수출단가 + MoM/YoY (관세청). 키 없거나 실패 시 {}.

    조회기간 1년 제약 때문에 2회 호출: 최근 5개월(최신월+전월) + 전년동월(YoY).
    최신 가용월은 통관 집계 lag(보통 1~2개월)로 가변이라, 반환된 월 중 최신을 쓴다.
    """
    if not _DATAGO_KEY:
        return {}
    now = kst_now()
    cur = now.year * 100 + now.month
    recent = _itemtrade(_yymm_shift(cur, -4), cur)   # 가용분만 반환됨(미래월은 빠짐)
    months = sorted({r["month"] for r in recent})
    if not months:
        return {}
    m_latest = months[-1]
    m_prev   = months[-2] if len(months) >= 2 else None
    y, mm    = m_latest.split(".")
    yoy_yymm = _yymm_shift(int(y) * 100 + int(mm), -12)
    yoy      = _itemtrade(yoy_yymm, yoy_yymm)
    yoy_key  = _yymm_to_dot(yoy_yymm)

    def find(rows, hs, month):
        return next((r for r in rows if r["hs"] == hs and r["month"] == month), None)

    def unit(r):
        return r["exp_dlr"] / r["exp_wgt"] if (r and r["exp_wgt"] > 0) else None

    def chg(cur_v, base_v):
        return (cur_v / base_v - 1) * 100 if (cur_v and base_v) else None

    out = {"month": m_latest, "items": {}}
    for hs, label in _MEM_HS.items():
        c  = find(recent, hs, m_latest)
        cu = unit(c)
        if not c or cu is None:
            continue
        p  = find(recent, hs, m_prev)
        yb = find(yoy, hs, yoy_key)
        out["items"][hs] = {
            "label":    label,
            "exp_dlr":  c["exp_dlr"],
            "unit":     cu,
            "mom_val":  chg(c["exp_dlr"], p["exp_dlr"]) if p else None,
            "mom_unit": chg(cu, unit(p)),
            "yoy_val":  chg(c["exp_dlr"], yb["exp_dlr"]) if yb else None,
            "yoy_unit": chg(cu, unit(yb)),
        }
    return out if out["items"] else {}


def format_memory_export_block(mem: dict) -> str:
    """fetch_kr_memory_export 결과 → 텍스트 블록(데이터 카드·Gemini 입력 공용). 빈 데이터면 ''."""
    if not mem or not mem.get("items"):
        return ""

    def pct(x):
        return f"{x:+.0f}%" if x is not None else "—"

    lines = [f"─── 🇰🇷 한국 메모리 수출 (관세청 {mem['month']}) ───"]
    for e in mem["items"].values():
        lines.append(
            f"• {e['label']}: ${e['exp_dlr'] / 1e9:.2f}B  "
            f"MoM {pct(e['mom_val'])} · YoY {pct(e['yoy_val'])}"
        )
        lines.append(
            f"    수출단가 ${e['unit']:,.0f}/kg  "
            f"MoM {pct(e['mom_unit'])} · YoY {pct(e['yoy_unit'])}"
        )
    return "\n".join(lines)


def fetch_semi_data() -> dict:
    """반도체 종목군 + ETF/지수 종가 + 변동률을 한 번에 수집.

    반환 스키마:
    {
      "indices": {name: {"price", "chg_pct"}},
      "groups":  {그룹명: [{"name", "ticker", "price", "chg_pct", "chg_5d", "chg_20d"}]},
      "top_gainers": [(name, group, chg_pct)],
      "top_losers":  [(name, group, chg_pct)],
    }
    """
    rows = _build_all_tickers()
    stock_tickers = [tk for _, tk, _ in rows]
    idx_tickers = list(INDICES_ETFS.values())
    all_tickers = stock_tickers + idx_tickers

    try:
        # 20거래일 대비도 보려면 최소 30일 필요. 휴장 대비 60일.
        raw = yf.download(
            tickers=all_tickers,
            period="60d",
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
    except Exception as e:
        log.error("yfinance 배치 다운로드 실패: %s", e)
        return {}

    multi = len(all_tickers) > 1
    result = {"indices": {}, "groups": {}, "top_gainers": [], "top_losers": []}

    # 지수·ETF 먼저
    for idx_name, idx_tk in INDICES_ETFS.items():
        s = _safe_close_series(raw, idx_tk, multi)
        if s is None:
            continue
        result["indices"][idx_name] = {
            "price":   float(s.iloc[-1]),
            "chg_pct": _pct_change(s) or 0.0,
            "chg_5d":  _change_over(s, 5),
            "chg_20d": _change_over(s, 20),
        }

    # 종목 그룹별 집계
    all_stock_list = []
    for name, tk, group in rows:
        s = _safe_close_series(raw, tk, multi)
        if s is None:
            continue
        chg = _pct_change(s)
        if chg is None:
            continue
        item = {
            "name":    name,
            "ticker":  tk,
            "price":   float(s.iloc[-1]),
            "chg_pct": chg,
            "chg_5d":  _change_over(s, 5),
            "chg_20d": _change_over(s, 20),
        }
        result["groups"].setdefault(group, []).append(item)
        all_stock_list.append((name, group, chg))

    # 그룹 내부 정렬: 일일 변동률 내림차순
    for g in result["groups"].values():
        g.sort(key=lambda x: x["chg_pct"], reverse=True)

    all_stock_list.sort(key=lambda x: x[2], reverse=True)
    result["top_gainers"] = all_stock_list[:5]
    result["top_losers"]  = all_stock_list[-5:][::-1]

    # 관세청 메모리 수출단가(보조 신호) — 실패해도 본 데이터엔 영향 없음
    try:
        result["memory_export"] = fetch_kr_memory_export()
    except Exception as e:
        log.warning("관세청 메모리 수출 수집 실패: %s", e)
        result["memory_export"] = {}

    return result


def build_semi_briefing_prompt(data_block: str, today_str: str) -> str:
    """반도체 업황 코멘트 프롬프트. 영상의 PER vs PBR 프레임을 그대로 차용."""
    # 페르소나·공통 가이드라인은 gemini.py 에서 import 시 순환 위험이라 inline 으로 둔다.
    return f"""당신은 CFA 자격을 보유한 국내 대형 증권사 리서치센터 반도체 담당 시니어 애널리스트입니다.
정량 데이터 인용에 충실하며, 모든 수치는 입력 데이터에 명시된 값만 그대로 사용합니다.

{today_str} 기준 반도체·메모리 업황 스크리닝 데이터:

{data_block}

다음 형식으로 정리하세요. 위 데이터에 **없는** 종목·지표·수치는 언급하지 마세요.

[🔬 반도체 업황 스크리닝 — {today_str}]

■ 시장 모멘텀 (^SOX·SMH·SOXX 기준)
- 1일/5일/20일 흐름의 강도와 방향 (3줄 이내)

■ 메모리 본진 (한국 005930·000660 + Micron·SanDisk·Kioxia)
- DRAM(005930·000660·MU) vs NAND 순수 플레이(SNDK·Kioxia) 흐름 차이
- 한·미 메모리 디커플링 여부 (수치 인용)

■ 빅테크 CAPEX 시그널 (MSFT·GOOGL·META·AMZN)
- 4사 주가 종합 흐름 — 메모리 수요 지속성 함의
- 이상치 종목 있으면 짚을 것

■ 평가 프레임 (영상 핵심)
- PBR(자산) 관점 vs PER(이익) 관점 — 현재 시장이 어느 쪽에 베팅 중인지 한 줄
- LTA(장기공급계약) 시그널 (수치로 확인 불가하면 "데이터로 직접 확인 불가" 명시)

■ 리스크 체크
- 데이터에 보이는 약세 신호 1~2가지 (지수 둔화·메모리 본진 약세·빅테크 약세 등)

[작성 원칙]
- 수치는 입력 데이터에 명시된 값만 인용. 추정·생성 금지.
- 데이터에 없는 항목은 "(데이터 없음)" 명시.
- 단정 피하고 조건부 표현: "X 가능성", "X 시사", "X 추정".
- 표·리스트 위주, 산문 최소화. 길이는 핵심만 짧게."""


def format_semi_data_block(data: dict) -> str:
    """fetch_semi_data 결과 → Gemini 입력용 텍스트 블록."""
    if not data or not data.get("groups"):
        return "데이터 없음"

    lines = ["── 지수·ETF ──"]
    for name, info in data.get("indices", {}).items():
        chg_5d = info.get("chg_5d")
        chg_20d = info.get("chg_20d")
        extra = []
        if chg_5d is not None:
            extra.append(f"5D {chg_5d:+.1f}%")
        if chg_20d is not None:
            extra.append(f"20D {chg_20d:+.1f}%")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"{name}: {info['price']:,.2f}  1D {info['chg_pct']:+.2f}%{suffix}")

    for group, items in data.get("groups", {}).items():
        lines.append("")
        lines.append(f"── {group} ──")
        for it in items:
            chg_5d = it.get("chg_5d")
            extra = f"  5D {chg_5d:+.1f}%" if chg_5d is not None else ""
            lines.append(
                f"{it['name']}({it['ticker']}): {it['price']:,.2f}  "
                f"1D {it['chg_pct']:+.2f}%{extra}"
            )

    if data.get("top_gainers"):
        lines.append("")
        lines.append("── 상승 TOP 5 ──")
        for name, group, chg in data["top_gainers"]:
            lines.append(f"{name} [{group}]: {chg:+.2f}%")

    if data.get("top_losers"):
        lines.append("")
        lines.append("── 하락 TOP 5 ──")
        for name, group, chg in data["top_losers"]:
            lines.append(f"{name} [{group}]: {chg:+.2f}%")

    mem_block = format_memory_export_block(data.get("memory_export"))
    if mem_block:
        lines += ["", mem_block]

    return "\n".join(lines)
