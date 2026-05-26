"""Fair Value Gap (FVG) Detector — ищет имбалансы на заданном таймфрейме."""


def detect_fvg(df, max_bars_back=10):
    """
    Ищет последний незакрытый FVG.

    Bullish FVG: Low[i+2] > High[i] — цена ушла вверх, оставив гэп
    Bearish FVG: High[i+2] < Low[i] — цена ушла вниз, оставив гэп

    Незакрытый = цена не возвращалась в зону FVG после его формирования.

    Returns: dict or None
        {
            'type': 'bullish' | 'bearish',
            'top': float,       # верхняя граница FVG
            'bottom': float,    # нижняя граница FVG
            'bar_index': int,   # индекс свечи, после которой FVG
            'active': bool
        }
    """
    if len(df) < 4:
        return None

    # Проходим от старых свечей к новым
    for i in range(min(max_bars_back, len(df) - 3), 0, -1):
        # i = индекс самой левой свечи в паттерне из 3
        bar0 = df.iloc[i]       # свеча 0
        bar1 = df.iloc[i + 1]   # свеча 1
        bar2 = df.iloc[i + 2]   # свеча 2

        # Bullish FVG: Low[2] > High[0]
        if bar2["low"] > bar0["high"]:
            top = bar2["low"]
            bottom = bar0["high"]
            fvg = {"type": "bullish", "top": top, "bottom": bottom, "bar_index": i + 2}

            # Проверяем, не закрыт ли FVG последующими свечами
            if not _is_filled(df, fvg, i + 3):
                fvg["active"] = True
                return fvg

        # Bearish FVG: High[2] < Low[0]
        if bar2["high"] < bar0["low"]:
            top = bar0["low"]
            bottom = bar2["high"]
            fvg = {"type": "bearish", "top": top, "bottom": bottom, "bar_index": i + 2}

            if not _is_filled(df, fvg, i + 3):
                fvg["active"] = True
                return fvg

    return None


def _is_filled(df, fvg, from_index):
    """Проверяет, перекрыла ли цена зону FVG после его формирования."""
    top = fvg["top"]
    bottom = fvg["bottom"]

    for j in range(from_index, len(df)):
        bar = df.iloc[j]
        if fvg["type"] == "bullish":
            # Цена вернулась вниз и коснулась верхней границы FVG
            if bar["low"] <= top:
                return True
        else:
            # Цена вернулась вверх и коснулась нижней границы FVG
            if bar["high"] >= bottom:
                return True

    return False


def check_fvg_signals(df_d1, df_h4):
    """
    Проверяет FVG на D1 и H4 для одной пары.
    Returns: {
        'd1': fvg_or_none,
        'h4': fvg_or_none,
        'd1_active': bool,
        'h4_active': bool,
        'direction': 'bullish'|'bearish'|None
    }
    """
    d1 = detect_fvg(df_d1)
    h4 = detect_fvg(df_h4)

    d1_active = d1 is not None and d1.get("active", False)
    h4_active = h4 is not None and h4.get("active", False)

    # Направление: оба должны смотреть в одну сторону
    direction = None
    if d1_active and h4_active:
        if d1["type"] == h4["type"]:
            direction = d1["type"]
    elif d1_active:
        direction = d1["type"]
    elif h4_active:
        direction = h4["type"]

    return {
        "d1": d1,
        "h4": h4,
        "d1_active": d1_active,
        "h4_active": h4_active,
        "direction": direction,
    }
