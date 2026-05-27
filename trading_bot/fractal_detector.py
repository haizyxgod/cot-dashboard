"""Williams Fractal Detector + ATR — динамические уровни Stop Loss."""
import pandas as pd


def calculate_atr(df, period=14):
    """Возвращает последнее значение ATR(14) на H4."""
    if len(df) < period + 1:
        return None
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(span=period, min_periods=period, adjust=False).mean()
    return atr.iloc[-1]


def find_fractals(df):
    """
    Находит все фракталы Williams на DataFrame.

    Up Fractal:   High[2] > High[0,1,3,4]
    Down Fractal: Low[2]  < Low[0,1,3,4]

    Returns: DataFrame с колонками ['up_fractal', 'down_fractal']
    """
    if len(df) < 5:
        return df.assign(up_fractal=None, down_fractal=None)

    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    up_fractals = [None] * n
    down_fractals = [None] * n

    for i in range(2, n - 2):
        # Up Fractal: high[i] > всех 4 соседей
        if (
            highs[i] > highs[i - 2]
            and highs[i] > highs[i - 1]
            and highs[i] > highs[i + 1]
            and highs[i] > highs[i + 2]
        ):
            up_fractals[i] = highs[i]

        # Down Fractal: low[i] < всех 4 соседей
        if (
            lows[i] < lows[i - 2]
            and lows[i] < lows[i - 1]
            and lows[i] < lows[i + 1]
            and lows[i] < lows[i + 2]
        ):
            down_fractals[i] = lows[i]

    return df.assign(up_fractal=up_fractals, down_fractal=down_fractals)


def nearest_fractal(df, direction, current_price, min_distance=None, atr_value=None):
    """
    Находит фрактал для SL с учётом ATR.

    direction: 'bullish' (BUY — down_fractal ниже цены)
               'bearish' (SELL — up_fractal выше цены)
    current_price: цена входа
    min_distance: мин. расстояние от входа в единицах цены.
                  Для forex 0.0015 = 15 pips, для gold 3.0 = $3.
                  Если None — авто: 0.0015 для цен < 100, иначе 3.0
    atr_value: значение ATR(14) на H4. Если задано — используется
               как нижний порог для min_distance (ATR * 0.75).

    Returns: float (цена фрактала) или None
    """
    if min_distance is None:
        min_distance = 3.0 if current_price > 100 else 0.0015

    # ATR floor: мин. дистанция не меньше 0.75 ATR
    if atr_value and atr_value > 0:
        atr_floor = atr_value * 0.75
        min_distance = max(min_distance, atr_floor)

    df = find_fractals(df)

    if direction == "bullish":
        candidates = df[df["down_fractal"].notna() & (df["down_fractal"] < current_price)]
        if candidates.empty:
            # No fractal — fallback to ATR-based SL
            if atr_value and atr_value > 0:
                return current_price - atr_value * 1.5
            return None
        candidates = candidates.sort_values("down_fractal", ascending=False)

        for _, row in candidates.iterrows():
            sl = row["down_fractal"]
            if (current_price - sl) >= min_distance:
                return sl
        # All fractals too close — use furthest one anyway (better than nothing)
        return candidates["down_fractal"].iloc[-1]

    elif direction == "bearish":
        candidates = df[df["up_fractal"].notna() & (df["up_fractal"] > current_price)]
        if candidates.empty:
            if atr_value and atr_value > 0:
                return current_price + atr_value * 1.5
            return None
        candidates = candidates.sort_values("up_fractal", ascending=True)

        for _, row in candidates.iterrows():
            sl = row["up_fractal"]
            if (sl - current_price) >= min_distance:
                return sl
        return candidates["up_fractal"].iloc[-1]

    return None
