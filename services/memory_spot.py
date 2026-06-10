"""
메모리(DRAM/NAND) 현물가 수집 — DRAMeXchange 공개 현물가 테이블 스크레이핑.

설계 원칙:
- 가격 숫자는 Python에서 결정론적으로 파싱한다(Gemini가 가격을 생성/환각하지 않게).
- 변동률은 자체 계산하지 않고 사이트의 Session Change(Daily High/Low/Average) 컬럼을
  그대로 쓴다. 현물가는 비거래일에 정체되므로 스크랩 diff로 '일변동'을 만들면 거짓이 된다.
- 외부 의존성 0(stdlib re/urllib). 시장 프록시는 기존 yfinance(services.semi 헬퍼 재사용).
- 구조가 깨지면 빈 결과를 반환해 호출측이 가시적으로 실패를 알리게 한다(조용한 no-op 금지).

데이터 출처: DRAMeXchange 무료 공개 페이지. 상업적 데이터 제공처라 마크업 변경/레이트리밋
가능 — 파서는 방어적으로, 실패는 드러나게.
"""
import logging
import re
import html as _html
import urllib.request
from datetime import datetime

import yfinance as yf
from services.semi import _safe_close_series, _pct_change

log = logging.getLogger(__name__)

DRAMX_URL = "https://www.dramexchange.com/"

# (raw 페이지 제품명 부분일치 키, 표시 라벨, 카테고리) — 큐레이션한 벤치마크 품목.
SPOT_ITEMS = [
    ("DDR5 16Gb (2Gx8) 4800/5600", "DDR5 16Gb", "DRAM"),
    ("DDR4 16Gb (2Gx8) 3200",      "DDR4 16Gb", "DRAM"),
    ("DDR4 8Gb (1Gx8) 3200",       "DDR4 8Gb",  "DRAM"),
    ("DDR5 RDIMM 32GB 4800/5600",  "DDR5 RDIMM 32GB(서버)", "모듈"),
    ("DDR5 UDIMM 16GB 4800/5600",  "DDR5 UDIMM 16GB", "모듈"),
    ("512Gb TLC",      "NAND 512Gb TLC", "NAND"),
    ("256Gb TLC",      "NAND 256Gb TLC", "NAND"),
    ("MLC 64Gb 8GBx8", "NAND MLC 64Gb",  "NAND"),
    ("GDDR6 8Gb",      "GDDR6 8Gb",      "GDDR"),
]

# 메모리 직접 노출 종목 — 현물가에 대한 시장(주가) 반응 참고용.
PROXY_TICKERS = [
    ("SK하이닉스",     "000660.KS"),
    ("삼성전자",       "005930.KS"),
    ("Micron",         "MU"),
    ("SanDisk",        "SNDK"),
    ("필라델피아 SOX", "^SOX"),
]

_WEEKDAY_KR = ['월', '화', '수', '목', '금', '토', '일']
_ARROW = {"up": "▲", "down": "▼", "flat": "—"}

_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_A = re.compile(r"<a[^>]*>(.*?)</a>", re.S)
# 값 셀은 class="tab_tr_gray"(숫자). 품목 셀 tab_tr_gray2, 변동 셀 tab_tr_font9 는 제외됨.
_NUM = re.compile(r'class="tab_tr_gray">\s*([\d.]+)\s*</td>')
_CHANGE = re.compile(r'class="tab_tr_font9">(.*?)</td>', re.S)


def _parse_spot_table(html: str) -> dict:
    """raw HTML → {제품명: {high, low, avg, change_pct, change_dir}}. 못 잡으면 빈 dict.

    한 행 구조: 품목(<a>) | Daily High | Daily Low | Session High | Session Low |
    Session Average | Session Change(up/down.gif + 'x.xx %') | History.
    값 셀 5개(High/Low/SesH/SesL/Avg) + 변동 셀을 추출한다.
    """
    out = {}
    for r in _ROW.findall(html):
        a = _A.search(r)
        if not a:
            continue
        name = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a.group(1)))).strip()
        nums = _NUM.findall(r)
        if not name or len(nums) < 5:
            continue
        chg = _CHANGE.search(r)
        change_pct = change_dir = None
        if chg:
            blob = chg.group(1)
            m = re.search(r"([\d.]+)\s*%", blob)
            change_pct = float(m.group(1)) if m else None
            change_dir = "up" if "up.gif" in blob else ("down" if "down.gif" in blob else "flat")
        out[name] = {
            "high": float(nums[0]), "low": float(nums[1]), "avg": float(nums[4]),
            "change_pct": change_pct, "change_dir": change_dir,
        }
    return out


def fetch_dram_spot() -> list[dict]:
    """DRAMeXchange 현물가 → SPOT_ITEMS 순서의 dict 리스트. 실패/미매칭 시 []."""
    try:
        req = urllib.request.Request(DRAMX_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "ignore")
    except Exception as e:
        log.warning("DRAMeXchange fetch 실패: %s", e)
        return []
    parsed = _parse_spot_table(html)
    rows = [{"label": label, "cat": cat, **parsed[raw]}
            for raw, label, cat in SPOT_ITEMS if raw in parsed]
    if not rows:
        log.warning("DRAMeXchange 파싱 0건 — 페이지 구조 변경 의심(parsed=%d행)", len(parsed))
    return rows


def fetch_memory_proxies() -> list[dict]:
    """메모리 노출 종목 전일대비 등락 — 현물가에 대한 시장 반응 참고. 실패 시 []."""
    tickers = [tk for _, tk in PROXY_TICKERS]
    try:
        raw = yf.download(tickers=tickers, period="7d", progress=False,
                          auto_adjust=True, group_by="ticker")
    except Exception as e:
        log.warning("프록시 yfinance 실패: %s", e)
        return []
    multi = len(tickers) > 1
    out = []
    for name, tk in PROXY_TICKERS:
        s = _safe_close_series(raw, tk, multi)
        chg = _pct_change(s)
        if s is None or chg is None:
            continue
        out.append({"name": name, "ticker": tk, "price": float(s.iloc[-1]), "chg_pct": chg})
    return out


def _fmt_pct(it: dict) -> str:
    """변동률 표시 문자열. 방향(up/down)으로 부호, 사이트 magnitude 사용. 없으면 ''."""
    if it["change_pct"] is None:
        return ""
    sign = {"up": "+", "down": "-", "flat": ""}.get(it["change_dir"], "")
    return f"{sign}{it['change_pct']:.2f}%"


def format_spot_card(spot: list[dict], proxies: list[dict], now: datetime) -> str:
    """Telegram용 결정론적 가격 카드(순수 함수 — 테스트 가능)."""
    lines = [
        "💾 **메모리 현물가** (DRAMeXchange)",
        f"📅 {now.strftime('%Y.%m.%d')} {_WEEKDAY_KR[now.weekday()]}  {now.strftime('%H:%M')}",
        "※ 현물가=소량 현물시장 시세(계약가와 별개), 단위 USD",
    ]
    last_cat = None
    for it in spot:
        if it["cat"] != last_cat:
            lines += ["", f"─── {it['cat']} ───"]
            last_cat = it["cat"]
        arr = _ARROW.get(it["change_dir"], "·")
        pct = _fmt_pct(it)
        pct_s = f"  ({pct})" if pct else ""
        lines.append(f"{arr} {it['label']}:  {it['avg']:g}{pct_s}   H{it['high']:g}/L{it['low']:g}")
    if proxies:
        lines += ["", "─── 📈 시장 프록시 (전일대비) ───"]
        for p in proxies:
            a = "▲" if p["chg_pct"] >= 0 else "▼"
            lines.append(f"{a} {p['name']}: {p['chg_pct']:+.2f}%")
    return "\n".join(lines)


def format_data_block(spot: list[dict], proxies: list[dict]) -> str:
    """Gemini 입력용 텍스트(숫자는 여기서 확정, Gemini는 코멘트만 생성)."""
    lines = ["── 현물가(USD, Session Average / 변동) ──"]
    for it in spot:
        pct = _fmt_pct(it) or "n/a"
        lines.append(f"[{it['cat']}] {it['label']}: {it['avg']:g} "
                     f"({pct}, H{it['high']:g}/L{it['low']:g})")
    if proxies:
        lines += ["", "── 시장 프록시(전일대비) ──"]
        for p in proxies:
            lines.append(f"{p['name']}: {p['chg_pct']:+.2f}%")
    return "\n".join(lines)


def build_memory_prompt(data_block: str, today_str: str) -> str:
    """현물가 코멘트 프롬프트. 숫자 생성 금지 — 입력 데이터 값만 인용."""
    return f"""당신은 메모리 반도체 현물시장을 추적하는 시니어 애널리스트입니다.
모든 수치는 아래 데이터에 명시된 값만 인용하고, 없는 값은 절대 생성하지 마세요.

{today_str} 메모리 현물가 데이터:

{data_block}

다음을 3~5줄로 짧게 코멘트하세요(표·산문 최소화, 핵심만):
- DRAM(DDR5 vs DDR4) 현물 흐름과 강도
- NAND(TLC 웨이퍼) 흐름
- 서버 모듈(RDIMM) 시그널
- 현물가 방향 vs 시장 프록시(주가) 일치/괴리
조건부 표현 사용("시사", "가능성", "추정"). 데이터에 없는 항목은 "(데이터 없음)" 명시."""
