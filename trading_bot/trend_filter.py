"""Daily trend filter — pair-specific EMA periods.

Gold:   EMA(50) vs EMA(200) — долгосрочный тренд
Forex: EMA(10) vs EMA(30)  — краткосрочный тренд (~2 нед / ~6 нед)
"""


def check_daily_trend(df_d1, pair_name=None):
    """
    Определяет направление тренда по EMA.

    pair_name: 'XAU/USD', 'USD/JPY', etc.
    Returns: 'bullish' | 'bearish' | 'neutral'
    """
    if pair_name and "JPY" in pair_name:
        fast, slow = 10, 30
        threshold = 0.3  # tighter for shorter EMAs
    else:
        fast, slow = 50, 200
        threshold = 0.5

    if len(df_d1) < slow:
        return "neutral"

    closes = df_d1["close"]
    ema_fast = closes.ewm(span=fast, min_periods=fast).mean().iloc[-1]
    ema_slow = closes.ewm(span=slow, min_periods=slow).mean().iloc[-1]

    if ema_slow <= 0 or ema_fast <= 0:
        return "neutral"

    diff_pct = abs(ema_fast - ema_slow) / ema_slow * 100

    if diff_pct < threshold:
        return "neutral"

    return "bullish" if ema_fast > ema_slow else "bearish"
