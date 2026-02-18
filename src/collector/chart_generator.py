"""Chart generator: ローソク足 + EMA + RSI + MACD + 出来高チャートを画像生成。

15m / 1h / 4h の3時間足に対応。
出力: data/charts/{symbol}_{timeframe}.png
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import mplfinance as mpf
import numpy as np
import pandas as pd

from src.utils.config_loader import get_data_dir, load_settings
from src.utils.file_lock import read_json
from src.utils.logger import setup_logger

logger = setup_logger("chart_generator")

CHARTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "charts"

# 時間足ごとの設定
TIMEFRAME_CONFIG = {
    "15m": {"label": "15m", "candle_key": "candles_15m"},
    "1h":  {"label": "1H",  "candle_key": "candles_1h"},
    "4h":  {"label": "4H",  "candle_key": "candles_4h"},
}


def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_macd(close: pd.Series):
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _calc_ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span).mean()


def generate_chart(symbol: str, candles: list, output_path: Path, timeframe: str = "15m") -> bool:
    """Generate a candlestick chart with EMA/MACD/RSI/Volume.

    Args:
        symbol: シンボル名 (例: "BTC")
        candles: ローソク足データのリスト
        output_path: 出力PNGパス
        timeframe: 時間足ラベル (例: "15m", "1H", "4H")

    Returns:
        True if chart was generated successfully.
    """
    if not candles or len(candles) < 5:
        logger.warning("Not enough candles for %s %s (%d)", symbol, timeframe, len(candles) if candles else 0)
        return False

    try:
        # Build DataFrame
        rows = []
        for c in candles:
            ts = pd.Timestamp(int(c["t"]), unit="ms", tz="UTC")
            rows.append({
                "Date": ts,
                "Open": float(c["o"]),
                "High": float(c["h"]),
                "Low": float(c["l"]),
                "Close": float(c["c"]),
                "Volume": float(c["v"]),
            })
        df = pd.DataFrame(rows)
        df.set_index("Date", inplace=True)

        if len(df) < 5:
            return False

        # Calculate indicators
        close = df["Close"]
        ema9 = _calc_ema(close, 9)
        ema21 = _calc_ema(close, 21)
        rsi = _calc_rsi(close, 14)
        macd_line, signal_line, macd_hist = _calc_macd(close)

        # Price change for title
        first_px = df["Close"].iloc[0]
        last_px = df["Close"].iloc[-1]
        chg_pct = (last_px - first_px) / first_px * 100

        # mplfinance style
        mc = mpf.make_marketcolors(
            up="#26a69a", down="#ef5350",
            edge="inherit", wick="inherit",
            volume={"up": "#26a69a80", "down": "#ef535080"},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            gridstyle="-", gridcolor="#e0e0e0",
            facecolor="white",
            rc={"font.size": 8},
        )

        # EMA overlays
        ema_plots = [
            mpf.make_addplot(ema9,  color="#2196F3", width=1.0, label="EMA9"),
            mpf.make_addplot(ema21, color="#FF9800", width=1.0, label="EMA21"),
        ]

        # RSI panel
        rsi_plot = [
            mpf.make_addplot(rsi, panel=2, color="#9C27B0", width=1.0, ylabel="RSI"),
            mpf.make_addplot(pd.Series(70, index=df.index), panel=2, color="red",   linestyle="--", width=0.5),
            mpf.make_addplot(pd.Series(30, index=df.index), panel=2, color="green", linestyle="--", width=0.5),
        ]

        # MACD panel
        macd_colors = ["#26a69a" if v >= 0 else "#ef5350" for v in macd_hist]
        macd_plot = [
            mpf.make_addplot(macd_line,   panel=3, color="#2196F3", width=1.0, ylabel="MACD"),
            mpf.make_addplot(signal_line, panel=3, color="#FF9800", width=1.0),
            mpf.make_addplot(macd_hist,   panel=3, type="bar", color=macd_colors, width=0.7),
        ]

        all_plots = ema_plots + rsi_plot + macd_plot

        output_path.parent.mkdir(parents=True, exist_ok=True)

        fig, axes = mpf.plot(
            df, type="candle", style=style,
            addplot=all_plots,
            volume=True,
            panel_ratios=(4, 1, 1.5, 1.5),
            figsize=(12, 8),
            title=f"{symbol}  {timeframe}  |  {last_px:,.2f}  ({chg_pct:+.1f}%)  |  {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            returnfig=True,
        )

        fig.savefig(str(output_path), dpi=100, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        logger.info("Chart generated: %s %s -> %s", symbol, timeframe, output_path)
        return True

    except Exception as e:
        logger.error("Chart generation failed for %s %s: %s", symbol, timeframe, e)
        return False


def generate_all_charts(settings: dict = None) -> dict[str, list[str]]:
    """Generate 15m/1h/4h charts for all symbols from market_data.json.

    Returns:
        dict mapping timeframe -> list of generated chart paths.
    """
    if settings is None:
        settings = load_settings()

    data_dir = get_data_dir(settings)
    market_data_path = data_dir / "market_data.json"

    try:
        data = read_json(market_data_path)
    except FileNotFoundError:
        logger.error("No market data found at %s", market_data_path)
        return {}

    result: dict[str, list[str]] = {tf: [] for tf in TIMEFRAME_CONFIG}
    symbols_data = data.get("symbols", {})

    for symbol, sym_data in symbols_data.items():
        for tf, cfg in TIMEFRAME_CONFIG.items():
            candles = sym_data.get(cfg["candle_key"], [])
            chart_path = CHARTS_DIR / f"{symbol}_{tf}.png"
            if generate_chart(symbol, candles, chart_path, timeframe=cfg["label"]):
                result[tf].append(str(chart_path))

    total = sum(len(v) for v in result.values())
    logger.info("Generated %d charts total (%s)", total,
                ", ".join(f"{tf}:{len(paths)}" for tf, paths in result.items()))
    return result


if __name__ == "__main__":
    charts = generate_all_charts()
    for tf, paths in charts.items():
        for p in paths:
            print(f"  [{tf}] {p}")
