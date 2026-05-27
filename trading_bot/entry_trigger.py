"""Entry Trigger — H1 fractal breakout or volatility surge.

После подтверждения COT + D1 FVG + Trend,
ждём триггер на H1 для точного входа:
  - Option A: H1 Williams Fractal в сторону сделки
  - Option B: H4 bar range > 1.5x avg range (всплеск волатильности)
"""

import config


def check_h1_trigger(df_h1, df_h4_recent, direction):
    """
    Проверяет условия входа на H1/H4.

    df_h1: последние 30 баров H1
    df_h4_recent: последние 5 баров H4 (для volatility surge)
    direction: 'BUY' or 'SELL'

    Returns: dict
        {
            'triggered': bool,
            'entry_price': float or None,
            'trigger_type': 'fractal' | 'volatility' | None,
            'bar_time': timestamp or None
        }
    """
    # --- Option A: H1 Fractal ---
    fractal = _check_h1_fractal(df_h1, direction)
    if fractal["found"]:
        return {
            "triggered": True,
            "entry_price": fractal["price"],
            "trigger_type": "h1_fractal",
            "bar_time": fractal["time"],
        }

    # --- Option B: Volatility surge on H4 ---
    vol = _check_volatility_surge(df_h4_recent)
    if vol["surge"]:
        # Entry at current close
        price = df_h4_recent.iloc[-1]["close"]
        return {
            "triggered": True,
            "entry_price": price,
            "trigger_type": "volatility",
            "bar_time": df_h4_recent.iloc[-1]["time"],
        }

    return {"triggered": False, "entry_price": None, "trigger_type": None, "bar_time": None}


def _check_h1_fractal(df_h1, direction):
    """Ищет свежий H1 фрактал (последние 5 баров) в сторону сделки."""
    if len(df_h1) < 7:
        return {"found": False, "price": None, "time": None}

    highs = df_h1["high"].values
    lows = df_h1["low"].values
    n = len(df_h1)

    # Проверяем последние 5 завершённых баров (индексы n-3 до n-6)
    for i in range(n - 3, max(n - 8, 1), -1):
        if i < 2 or i > n - 3:
            continue

        if direction == "BUY":
            # Up fractal: high[i] > всех 4 соседей → пробой вверх = BUY сигнал
            if (highs[i] > highs[i - 2] and highs[i] > highs[i - 1]
                    and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]):
                # Вход: на пробое high фрактала (берём high фрактала + 1 пункт)
                entry = highs[i]
                return {"found": True, "price": entry, "time": str(df_h1.iloc[i]["time"])}
        else:
            # Down fractal: low[i] < всех 4 соседей → пробой вниз = SELL сигнал
            if (lows[i] < lows[i - 2] and lows[i] < lows[i - 1]
                    and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]):
                entry = lows[i]
                return {"found": True, "price": entry, "time": str(df_h1.iloc[i]["time"])}

    return {"found": False, "price": None, "time": None}


def _check_volatility_surge(df_h4):
    """Проверяет всплеск волатильности: range > 1.5x средний range."""
    if len(df_h4) < 10:
        return {"surge": False}

    ranges = df_h4["high"].values - df_h4["low"].values
    avg_range = ranges[:-1].mean()  # exclude current bar
    current_range = ranges[-1]

    if avg_range > 0 and current_range > avg_range * config.H1_ATR_SURGE_MULT:
        return {"surge": True}
    return {"surge": False}
