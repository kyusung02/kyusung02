"""
섹터 브리핑 핸들러 — 국내 증시 섹터별 자동 브리핑 (평일 10/12/14/16시)
"""
import logging
import asyncio
import yfinance as yf
from clients import bot_client, _executor
from config import BROADCAST_ID
from utils import kst_now, kst_today
from services.gemini import generate_with_retry, build_kr_close_comment_prompt

log = logging.getLogger(__name__)

_WEEKDAY_KR = ['월요일', '화요일', '수요일', '목요일', '금요일', '토요일', '일요일']

KR_SECTORS = {
    "반도체": [
        ("삼성전자",    "005930.KS"), ("SK하이닉스",  "000660.KS"),
        ("한미반도체",  "042700.KS"), ("리노공업",    "058470.KS"),
        ("이오테크닉스","039030.KS"), ("원익IPS",     "240810.KS"),
        ("하나마이크론","067310.KS"), ("피에스케이",  "319660.KQ"),
        ("DB하이텍",    "000990.KS"), ("HPSP",        "403870.KQ"),
    ],
    "이차전지": [
        ("LG에너지솔루션","373220.KS"), ("삼성SDI",    "006400.KS"),
        ("SK이노베이션",  "096770.KS"), ("에코프로비엠","247540.KQ"),
        ("에코프로",      "086520.KQ"), ("포스코퓨처엠","003670.KS"),
        ("엘앤에프",      "066970.KQ"), ("코스모신소재","005070.KS"),
    ],
    "바이오": [
        ("삼성바이오로직스","207940.KS"), ("셀트리온",  "068270.KS"),
        ("한미약품",        "128940.KS"), ("유한양행",  "000100.KS"),
        ("HLB",             "028300.KQ"), ("알테오젠",  "196170.KQ"),
        ("종근당",          "185750.KS"), ("대웅제약",  "069620.KS"),
    ],
    "자동차": [
        ("현대차",    "005380.KS"), ("기아",       "000270.KS"),
        ("현대모비스","012330.KS"), ("HL만도",     "204320.KS"),
        ("현대위아",  "011210.KS"), ("한온시스템", "018880.KS"),
    ],
    "금융": [
        ("KB금융",         "105560.KS"), ("신한지주",      "055550.KS"),
        ("하나금융지주",   "086790.KS"), ("우리금융지주",  "316140.KS"),
        ("삼성화재",       "000810.KS"), ("메리츠금융지주","138040.KS"),
    ],
    "증권": [
        ("미래에셋증권","006800.KS"), ("삼성증권",  "016360.KS"),
        ("한국투자증권","071050.KS"), ("NH투자증권","005940.KS"),
        ("키움증권",    "039490.KS"),
    ],
    "방산": [
        ("한화에어로스페이스","012450.KS"), ("LIG넥스원",   "079550.KS"),
        ("현대로템",          "064350.KS"), ("한국항공우주","047810.KS"),
        ("한화시스템",        "272210.KS"),
    ],
    "조선": [
        ("HD한국조선해양","009540.KS"), ("한화오션",    "042660.KS"),
        ("삼성중공업",    "010140.KS"), ("HD현대중공업","329180.KS"),
        ("HD현대미포",    "010620.KS"),
    ],
    "원전/에너지": [
        ("두산에너빌리티","034020.KS"), ("한전KPS",  "051600.KS"),
        ("한전기술",      "052690.KS"), ("한국전력", "015760.KS"),
    ],
    "철강/소재": [
        ("POSCO홀딩스","005490.KS"), ("현대제철","004020.KS"),
        ("고려아연",   "010130.KS"), ("풍산",    "103140.KS"),
    ],
    "화학": [
        ("LG화학",   "051910.KS"), ("롯데케미칼",  "011170.KS"),
        ("금호석유", "011780.KS"), ("효성첨단소재","298050.KS"),
    ],
    "건설": [
        ("삼성물산","028260.KS"), ("현대건설","000720.KS"),
        ("GS건설",  "006360.KS"), ("대우건설", "047040.KS"),
        ("DL이앤씨","375500.KS"),
    ],
    "통신": [
        ("SK텔레콤", "017670.KS"), ("KT",         "030200.KS"),
        ("LG유플러스","032640.KS"),
    ],
    "인터넷/플랫폼": [
        ("NAVER",     "035420.KS"), ("카카오",    "035720.KS"),
        ("카카오뱅크","323410.KS"),
    ],
    "게임": [
        ("크래프톤",    "259960.KS"), ("넷마블",     "251270.KS"),
        ("엔씨소프트",  "036570.KS"), ("펄어비스",   "263750.KQ"),
        ("카카오게임즈","293490.KQ"),
    ],
    "엔터/미디어": [
        ("HYBE",          "352820.KS"), ("SM엔터테인먼트","041510.KQ"),
        ("JYP Ent.",       "035900.KQ"), ("YG엔터테인먼트","122870.KQ"),
    ],
    "로봇/AI": [
        ("레인보우로보틱스","277810.KQ"), ("두산로보틱스","454910.KS"),
        ("에스피지",        "073070.KQ"), ("로보티즈",    "108490.KQ"),
    ],
}

_INDEX_TICKERS = {
    "코스피": "^KS11",
    "코스닥": "^KQ11",
}


def _fetch_sector_data() -> dict:
    ticker_to_meta = {}
    for sector, stocks in KR_SECTORS.items():
        for name, ticker in stocks:
            ticker_to_meta[ticker] = (sector, name)

    all_tickers = list(ticker_to_meta.keys()) + list(_INDEX_TICKERS.values())
    try:
        raw = yf.download(
            tickers=all_tickers,
            period="5d",
            progress=False,
            auto_adjust=True,
            group_by="ticker",
        )
    except Exception as e:
        log.error("yfinance 배치 다운로드 실패: %s", e)
        return {}

    def _series(ticker: str):
        try:
            if len(all_tickers) > 1:
                if ticker not in raw.columns.get_level_values(0):
                    return None
                s = raw[ticker]["Close"].dropna()
            else:
                s = raw["Close"].dropna()
            return s if len(s) >= 1 else None
        except Exception:
            return None

    # 배치 다운로드된 모든 종목 날짜의 합집합 = 가장 완전한 '거래일 달력'.
    # 개별 종목 바가 누락돼도(예: HPSP 06-16) 다른 종목·지수가 그 날을 채운다.
    series_map   = {t: _series(t) for t in all_tickers}
    trading_days = sorted({d.date() for s in series_map.values()
                           if s is not None for d in s.index})

    def _day_change(ticker: str):
        """(cur, prev) 반환. prev는 '직전 거래일' 종가.

        종목 바가 누락돼 직전 거래일 종가가 시리즈에 없으면(예: HPSP 06-16),
        iloc[-2]가 이틀 전을 집어 다중일 변동률이 '전일 대비'로 둔갑한다.
        이 경우 yfinance 공식 previous_close로 보정하고, 그래도 없으면 None.
        """
        s = series_map.get(ticker)
        if s is None or len(s) < 1:
            return None
        cur     = float(s.iloc[-1])
        cur_day = s.index[-1].date()
        priors  = [d for d in trading_days if d < cur_day]
        if not priors:
            return None
        expected_prev = priors[-1]
        if len(s) >= 2 and s.index[-2].date() == expected_prev:
            return cur, float(s.iloc[-2])
        # 직전 거래일 바 누락 → 공식 전일종가로 보정 (갭난 종목만 추가 호출)
        try:
            pc = float(yf.Ticker(ticker).fast_info.previous_close)
            if pc > 0:
                return cur, pc
        except Exception:
            pass
        log.warning("%s 직전 거래일(%s) 종가 누락 — 변동률 생략", ticker, expected_prev)
        return None

    result = {"sectors": {}, "indices": {}, "top_gainers": [], "top_losers": []}

    for idx_name, idx_ticker in _INDEX_TICKERS.items():
        dc = _day_change(idx_ticker)
        if dc is None:
            continue
        cur, prev = dc
        s = series_map.get(idx_ticker)
        result["indices"][idx_name] = {
            "price":   cur,
            "chg_pct": (cur - prev) / prev * 100,
            # 최신 바 날짜 — 장 마감 총평의 '당일 종가 확정' 검증에 사용
            "date":    s.index[-1].date() if s is not None and len(s) else None,
        }

    all_stock_list = []
    for sector, stocks in KR_SECTORS.items():
        changes, stock_rows = [], []
        for name, ticker in stocks:
            dc = _day_change(ticker)
            if dc is None:
                continue
            cur, prev = dc
            chg = (cur - prev) / prev * 100
            changes.append(chg)
            stock_rows.append((name, chg))
            all_stock_list.append((name, sector, chg))
        if not changes:
            continue
        result["sectors"][sector] = {
            "avg_chg":     sum(changes) / len(changes),
            "up_pct":      sum(1 for c in changes if c > 0) / len(changes) * 100,
            "stock_count": len(changes),
            "stocks":      sorted(stock_rows, key=lambda x: x[1], reverse=True),
        }

    all_stock_list.sort(key=lambda x: x[2], reverse=True)
    result["top_gainers"] = all_stock_list[:5]
    result["top_losers"]  = all_stock_list[-5:][::-1]
    return result


def _format_message(data: dict) -> str:
    now     = kst_now()
    weekday = _WEEKDAY_KR[now.weekday()]
    lines   = [
        "🇰🇷 **국내 증시 섹터별 브리핑**",
        f"📅 {now.strftime('%Y.%m.%d')} {weekday}  {now.strftime('%H:%M')}",
        "",
        "─── 📊 주요 지수 ───",
    ]
    for name, info in data.get("indices", {}).items():
        arrow = "▲" if info["chg_pct"] >= 0 else "▼"
        sign  = "+" if info["chg_pct"] >= 0 else ""
        lines.append(f"{arrow} {name}: {info['price']:,.2f}  ({sign}{info['chg_pct']:.2f}%)")

    lines.append("")
    lines.append("─── 📈 섹터별 성과 ───")
    for s_name, info in sorted(data.get("sectors", {}).items(),
                                key=lambda x: x[1]["avg_chg"], reverse=True):
        avg = info["avg_chg"]
        dot = "🟢" if avg >= 0 else "🔴"
        arr = "▲" if avg >= 0 else "▼"
        lines.append(
            f"{dot} {s_name:<12}  {arr}{abs(avg):.1f}%   "
            f"상승 {info['up_pct']:.0f}% ({info['stock_count']}종목)"
        )

    lines += ["", "─── 🏆 상승 Top 5 ───"]
    for i, (name, sector, chg) in enumerate(data.get("top_gainers", []), 1):
        lines.append(f"{i}. {name}  ▲{chg:.1f}%  [{sector}]")

    lines += ["", "─── 💥 하락 Top 5 ───"]
    for i, (name, sector, chg) in enumerate(data.get("top_losers", []), 1):
        lines.append(f"{i}. {name}  ▼{abs(chg):.1f}%  [{sector}]")

    return "\n".join(lines)


def _build_close_summary(data: dict) -> str:
    """Gemini 마감 총평용 결정론적 지표 요약 (수치는 전부 Python이 계산)."""
    lines = []
    for name, info in data.get("indices", {}).items():
        lines.append(f"{name} 종가 {info['price']:,.2f} ({info['chg_pct']:+.2f}%)")
    secs = sorted(data.get("sectors", {}).items(),
                  key=lambda x: x[1]["avg_chg"], reverse=True)
    if secs:
        top = ", ".join(f"{n}({i['avg_chg']:+.1f}%)" for n, i in secs[:3])
        bot = ", ".join(f"{n}({i['avg_chg']:+.1f}%)" for n, i in secs[-3:])
        lines.append(f"강세 섹터: {top}")
        lines.append(f"약세 섹터: {bot}")
    g = data.get("top_gainers", [])[:3]
    losers = data.get("top_losers", [])[:3]
    if g:
        lines.append("상승 상위: " + ", ".join(f"{n}(+{c:.1f}%)" for n, _s, c in g))
    if losers:
        lines.append("하락 상위: " + ", ".join(f"{n}({c:.1f}%)" for n, _s, c in losers))
    return "\n".join(lines)


async def send_kr_sector_briefing():
    """평일 10/12/14/16시 자동 실행 + /섹터 명령어로 수동 호출 가능.

    16시 이후(장 마감) 실행에는 Gemini 마감 총평을 앞에 얹는다. 단 당일 종가 바가
    아직 yfinance에 확정되지 않았으면(지연) '오늘 마감'을 단언하지 않고 경고로 대체한다
    — 모닝 시황 stale 사고(06-23 폭락이 06-22로 둔갑)의 반대 방향 재발 방지.
    """
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(_executor, _fetch_sector_data)
        if not data or not data.get("sectors"):
            await bot_client.send_message(BROADCAST_ID, "⚠️ 섹터 데이터 조회에 실패했습니다.")
            return

        msg = _format_message(data)

        now = kst_now()
        if now.hour >= 16:   # 장 마감(15:30) 이후 — 16:05 정기 실행/수동 호출
            kospi = data.get("indices", {}).get("코스피", {})
            if kospi.get("date") != kst_today():
                msg = ("⚠️ **마감 데이터 확정 대기** — 당일 종가가 아직 반영되지 않아 "
                       "아래 수치는 직전 거래일 기준일 수 있습니다.\n\n") + msg
            else:
                try:
                    summary = _build_close_summary(data)
                    prompt  = build_kr_close_comment_prompt(
                        summary, now.strftime('%Y년 %m월 %d일'))
                    resp = await loop.run_in_executor(_executor, generate_with_retry, [prompt])
                    text = getattr(resp, "text", "") or ""
                    if text.strip():
                        msg = text.strip() + "\n\n" + msg
                except Exception as e:
                    log.warning("마감 총평 생성 실패(섹터 본문은 발송): %s", e)

        # Gemini 마감 총평이 섞이면 마크다운 quirk로 파싱 실패 가능 → plain 폴백으로
        # 정상 섹터 본문까지 유실되는 사고 방지(메모: "Gemini 출력 plain 전송").
        try:
            await bot_client.send_message(BROADCAST_ID, msg, parse_mode="md")
        except Exception as e:
            log.warning("마크다운 발송 실패 → 플레인 재시도: %s", e)
            await bot_client.send_message(BROADCAST_ID, msg)
        log.info("섹터 브리핑 전송 완료")
    except Exception as e:
        log.error("섹터 브리핑 전송 실패: %s", e)
        await bot_client.send_message(BROADCAST_ID, "⚠️ 섹터 브리핑 생성 중 오류가 발생했습니다.")
