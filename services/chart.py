"""
차트 생성 — yfinance 데이터로 주가/실적/투자자 흐름 차트 PNG 저장
"""
import re
import logging
import urllib.request
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import yfinance as yf
from services.stock import US_STOCK_MAP, is_korean_ticker
from utils import safe_opener

log = logging.getLogger(__name__)


def find_row(df: pd.DataFrame, candidates: list[str]):
    """yfinance income_stmt에서 후보 라벨 중 첫 매칭 row 반환."""
    for name in candidates:
        if name in df.index:
            return df.loc[name]
    return None


def quarter_label(dt) -> str:
    """datetime → 'YYYYQn' 분기 라벨."""
    return f"{dt.year}Q{(dt.month - 1) // 3 + 1}"


def financial_unit(ticker: str) -> tuple[float, str]:
    """ticker별 금액 단위 변환계수와 표시 라벨."""
    if is_korean_ticker(ticker):
        return 1e12, '조원'
    return 1e9, 'B$'


# 캔들/거래량 색 — 국내 관례(상승 빨강, 하락 파랑). 변경 시 여기만 수정.
_CANDLE_UP   = '#d32f2f'
_CANDLE_DOWN = '#1976d2'

# (period, interval, 날짜 포맷, 장기 MA, 제목 접미사) — 일봉 3개월 / 주봉 5년 공통 스펙
_PRICE_CHART_SPECS = [
    ('3mo', '1d',  '%m/%d', 60,  'Daily (3M)'),
    ('5y',  '1wk', '%Y/%m', 200, 'Weekly (5Y)'),
]


def _draw_price_volume(df, title: str, date_fmt: str, ylabel: str, ma_long: int, path: str):
    """가격(MA 5/20/장기) + 거래량 2단 차트 저장 — KR/US 일봉·주봉 공용."""
    df = df.copy()
    df['MA_S'] = df['Close'].rolling(5).mean()
    df['MA_M'] = df['Close'].rolling(20).mean()
    df['MA_L'] = df['Close'].rolling(ma_long).mean()

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                    gridspec_kw={'height_ratios': [3, 1]})
    fig.suptitle(title, fontsize=13)
    ax1.plot(df.index, df['Close'], linewidth=1.5, color='#1f77b4', label='Close')
    ax1.plot(df.index, df['MA_S'], linewidth=1, color='orange', alpha=0.9, label='MA5')
    ax1.plot(df.index, df['MA_M'], linewidth=1, color='green',  alpha=0.9, label='MA20')
    ax1.plot(df.index, df['MA_L'], linewidth=1, color='red',    alpha=0.9, label=f'MA{ma_long}')
    ax1.legend(loc='upper left', fontsize=8)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
    ax1.set_ylabel(ylabel)
    ax1.grid(True, alpha=0.3)

    colors = [_CANDLE_UP if c >= o else _CANDLE_DOWN
              for c, o in zip(df['Close'], df['Open'])]
    ax2.bar(df.index, df['Volume'], color=colors, alpha=0.7)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
    ax2.set_ylabel('Volume')
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(path, dpi=100, bbox_inches='tight')
    plt.close()


def _draw_chart_kr(ticker: str, company: str, path_daily: str, path_weekly: str):
    """일봉(3개월) + 주봉(5년) 차트 파일 저장 (동기 함수 - executor에서 실행)"""
    stock = yf.Ticker(ticker)
    for (period, interval, fmt, ma_long, suffix), path in zip(
            _PRICE_CHART_SPECS, [path_daily, path_weekly]):
        df = stock.history(period=period, interval=interval)
        if df.empty:
            continue
        _draw_price_volume(df, f'{company} {suffix}', fmt, 'Price (KRW)', ma_long, path)


def _draw_chart_us(ticker_raw: str, path_daily: str, path_weekly: str):
    """미국 종목 일봉(3개월) + 주봉(5년) 차트 저장 (동기 함수 - executor에서 실행)"""
    ticker = US_STOCK_MAP.get(ticker_raw.lower(), US_STOCK_MAP.get(ticker_raw, ticker_raw.upper()))
    stock  = yf.Ticker(ticker)
    for (period, interval, fmt, ma_long, suffix), path in zip(
            _PRICE_CHART_SPECS, [path_daily, path_weekly]):
        df = stock.history(period=period, interval=interval)
        if df.empty:
            continue
        _draw_price_volume(df, f'{ticker} {suffix}', fmt, 'Price (USD)', ma_long, path)


def _draw_chart_financials(ticker_sym: str, company: str, path: str):
    """분기 실적 차트 생성 (동기 함수 - executor에서 실행)"""
    try:
        stock = yf.Ticker(ticker_sym)
        df = stock.quarterly_income_stmt
        if df is None or df.empty:
            return

        df = df.sort_index(axis=1).iloc[:, -8:]

        rev = find_row(df, ['Total Revenue', 'Revenue'])
        oi  = find_row(df, ['Operating Income', 'EBIT', 'Operating Income Loss'])
        ni  = find_row(df, ['Net Income', 'Net Income Common Stockholders'])

        if rev is None:
            return

        unit, ulabel = financial_unit(ticker_sym)
        labels = [quarter_label(c) for c in df.columns]

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

            for bar, v in zip(bars, vals):
                if v == 0:
                    continue
                ypos = bar.get_height()
                ax.text(bar.get_x() + bar.get_width() / 2,
                        ypos + (abs(vals.max() - vals.min()) * 0.02),
                        f'{v:.1f}', ha='center', va='bottom', fontsize=7.5)

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


def _draw_report_financials(ticker: str, company: str, path_pnl: str, path_ttm_rev: str, path_ttm_op: str):
    """분기 손익 차트 3종 저장 (동기 함수 - executor에서 실행)"""
    try:
        stock = yf.Ticker(ticker)
        df = stock.quarterly_income_stmt
        if df is None or df.empty:
            return
        df = df.sort_index(axis=1).iloc[:, -8:]

        rev = find_row(df, ['Total Revenue', 'Revenue'])
        oi  = find_row(df, ['Operating Income', 'EBIT', 'Operating Income Loss'])
        if rev is None:
            return

        unit, ulabel = financial_unit(ticker)
        labels = [quarter_label(c) for c in df.columns]
        rev_vals = pd.to_numeric(rev, errors='coerce').fillna(0) / unit
        oi_vals  = pd.to_numeric(oi,  errors='coerce').fillna(0) / unit if oi is not None else None

        x     = list(range(len(labels)))
        width = 0.35

        fig, ax1 = plt.subplots(figsize=(10, 5))
        ax1.bar([i - width / 2 for i in x], rev_vals, width, label=f'매출 ({ulabel})',    color='#4C72B0', alpha=0.85)
        ax1.bar([i + width / 2 for i in x], oi_vals,  width, label=f'영업이익 ({ulabel})', color='#DD8452', alpha=0.85)
        ax1.set_xticks(x)
        ax1.set_xticklabels(labels, fontsize=8, rotation=30)
        ax1.set_ylabel(f'({ulabel})')
        ax1.legend(loc='upper left', fontsize=8)
        ax1.grid(axis='y', alpha=0.3)
        ax1.axhline(0, color='black', linewidth=0.8)

        if oi_vals is not None:
            opm_vals = [(o / r * 100) if r != 0 else 0 for o, r in zip(oi_vals, rev_vals)]
            ax2 = ax1.twinx()
            ax2.plot(x, opm_vals, color='red', linewidth=1.5, linestyle='--',
                     marker='o', markersize=4, label='OPM%')
            ax2.set_ylabel('OPM (%)', color='red')
            ax2.tick_params(axis='y', labelcolor='red')
            ax2.legend(loc='upper right', fontsize=8)

        fig.suptitle(f'{company} 분기별 손익', fontsize=13, fontweight='bold')
        plt.tight_layout()
        plt.savefig(path_pnl, dpi=100, bbox_inches='tight')
        plt.close()

        if len(rev_vals) >= 4:
            ttm_rev = [rev_vals.iloc[i - 3:i + 1].sum() for i in range(3, len(rev_vals))]
            ttm_lbl = labels[3:]
            fig, ax = plt.subplots(figsize=(10, 4))
            ax.plot(range(len(ttm_lbl)), ttm_rev, color='#4C72B0', linewidth=2, marker='o', markersize=5)
            ax.fill_between(range(len(ttm_lbl)), ttm_rev, alpha=0.15, color='#4C72B0')
            for i, v in enumerate(ttm_rev):
                ax.text(i, v, f'{v:.1f}', ha='center', va='bottom', fontsize=8)
            ax.set_xticks(range(len(ttm_lbl)))
            ax.set_xticklabels(ttm_lbl, fontsize=8, rotation=30)
            ax.set_ylabel(f'TTM 매출 ({ulabel})')
            ax.grid(True, alpha=0.3)
            fig.suptitle(f'{company} TTM 매출 추이', fontsize=13, fontweight='bold')
            plt.tight_layout()
            plt.savefig(path_ttm_rev, dpi=100, bbox_inches='tight')
            plt.close()

        if oi_vals is not None and len(oi_vals) >= 4:
            ttm_oi  = [oi_vals.iloc[i - 3:i + 1].sum() for i in range(3, len(oi_vals))]
            ttm_lbl = labels[3:]
            pos_col = '#55A868'
            neg_col = '#d32f2f'
            colors  = [neg_col if v < 0 else pos_col for v in ttm_oi]
            fig, ax = plt.subplots(figsize=(10, 4))
            for i in range(len(ttm_lbl) - 1):
                c = neg_col if ttm_oi[i] < 0 or ttm_oi[i + 1] < 0 else pos_col
                ax.plot([i, i + 1], [ttm_oi[i], ttm_oi[i + 1]], color=c, linewidth=2)
            ax.scatter(range(len(ttm_lbl)), ttm_oi, color=colors, zorder=5, s=50)
            for i, v in enumerate(ttm_oi):
                ax.text(i, v, f'{v:.1f}', ha='center',
                        va='bottom' if v >= 0 else 'top', fontsize=8)
            ax.fill_between(range(len(ttm_lbl)), ttm_oi, 0,
                            where=[v >= 0 for v in ttm_oi], alpha=0.1, color=pos_col)
            ax.fill_between(range(len(ttm_lbl)), ttm_oi, 0,
                            where=[v < 0 for v in ttm_oi],  alpha=0.1, color=neg_col)
            ax.axhline(0, color='black', linewidth=0.8, linestyle='--')
            ax.set_xticks(range(len(ttm_lbl)))
            ax.set_xticklabels(ttm_lbl, fontsize=8, rotation=30)
            ax.set_ylabel(f'TTM 영업이익 ({ulabel})')
            ax.grid(True, alpha=0.3)
            fig.suptitle(f'{company} TTM 영업이익 추이', fontsize=13, fontweight='bold')
            plt.tight_layout()
            plt.savefig(path_ttm_op, dpi=100, bbox_inches='tight')
            plt.close()

    except Exception as e:
        log.warning("_draw_report_financials failed for %s: %s", ticker, e)
        try:
            plt.close()
        except Exception:
            pass


def _draw_chart_investor_flow(stock_code: str, company: str, path: str):
    """외국인 누적 순매수 차트 저장 - 네이버 금융 크롤링 (동기 함수 - executor에서 실행).

    stock_code는 6자리 숫자만 허용 (URL 인젝션 방지).
    """
    if not re.fullmatch(r'\d{6}', stock_code or ''):
        log.warning("_draw_chart_investor_flow: 잘못된 stock_code 형식 — %s", stock_code)
        return
    try:
        all_dates: list = []
        all_net: list   = []

        for page in range(1, 14):
            url = f"https://finance.naver.com/item/frgn.nhn?code={stock_code}&page={page}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with safe_opener.open(req, timeout=8) as resp:
                    html = resp.read().decode('euc-kr', errors='ignore')
            except Exception:
                break

            found_any = False
            for row_html in html.split('<tr>'):
                cells = re.findall(r'<td[^>]*>([\s\S]*?)</td>', row_html)
                cells = [re.sub(r'<[^>]+>', '', c).strip().replace(',', '').replace('\xa0', '') for c in cells]
                cells = [c for c in cells if c]
                if not cells or not re.match(r'^\d{4}\.\d{2}\.\d{2}$', cells[0]):
                    continue
                try:
                    d = datetime.strptime(cells[0], '%Y.%m.%d')
                except ValueError:
                    continue
                if len(cells) < 5:
                    continue
                raw = cells[4].replace('+', '').strip()
                if not raw or raw == '-':
                    continue
                try:
                    net = int(raw)
                    all_dates.append(d)
                    all_net.append(net)
                    found_any = True
                except ValueError:
                    continue

            if not found_any:
                break

        if len(all_dates) < 5:
            return

        pairs     = sorted(zip(all_dates, all_net))
        all_dates = [p[0] for p in pairs][-120:]
        all_net   = [p[1] for p in pairs][-120:]

        cum, total = [], 0
        for v in all_net:
            total += v
            cum.append(total)
        # 차트 시작점을 0으로 상대화 (이후 누적 등락의 절대값이 아닌 추세를 보여줌)
        if cum:
            base = cum[0]
            cum  = [v - base for v in cum]

        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(all_dates, cum, color='#d32f2f', linewidth=1.5, label='외국인 누적순매수')
        ax.fill_between(all_dates, cum, 0,
                        where=[v >= 0 for v in cum], alpha=0.15, color='#d32f2f')
        ax.fill_between(all_dates, cum, 0,
                        where=[v < 0  for v in cum], alpha=0.15, color='#1565C0')
        ax.axhline(0, color='black', linewidth=0.8, linestyle='--')
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.set_ylabel('누적 순매수 (주)')
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)
        fig.suptitle(f'{company} 외국인 누적 순매수 (6개월)', fontsize=13, fontweight='bold')
        plt.tight_layout()
        plt.savefig(path, dpi=100, bbox_inches='tight')
        plt.close()
    except Exception as e:
        log.warning("_draw_chart_investor_flow failed: %s", e)
        try:
            plt.close()
        except Exception:
            pass
