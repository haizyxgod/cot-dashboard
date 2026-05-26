"""Williams Fractal Detector — для определения уровней Stop Loss."""


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


def nearest_fractal(df, direction, current_price):
    """
    Находит ближайший фрактал для SL.

    direction: 'bullish' (для BUY — ищем down_fractal ниже цены)
               'bearish' (для SELL — ищем up_fractal выше цены)
    current_price: текущая цена входа

    Returns: float (цена фрактала) или None
    """
    df = find_fractals(df)

    if direction == "bullish":
        # Ищем ближайший down_fractal НИЖЕ текущей цены
        candidates = df[df["down_fractal"].notna() & (df["down_fractal"] < current_price)]
        if candidates.empty:
            return None
        # Ближайший к цене (максимальный из тех что ниже)
        return candidates["down_fractal"].max()

    elif direction == "bearish":
        # Ищем ближайший up_fractal ВЫШЕ текущей цены
        candidates = df[df["up_fractal"].notna() & (df["up_fractal"] > current_price)]
        if candidates.empty:
            return None
        # Ближайший к цене (минимальный из тех что выше)
        return candidates["up_fractal"].min()

    return None
