"""Signal Engine — матрица принятия решений COT + FVG."""


def evaluate_setup(fvg_result, cot_verdict):
    """
    Применяет риск-матрицу к результатам FVG + COT.

    fvg_result: dict из fvg_detector.check_fvg_signals()
    cot_verdict: dict из COT engine с ключами: signal, score, direction

    Returns: dict
        {
            'trade': bool,         # Есть ли сигнал
            'direction': str,      # 'BUY' | 'SELL' | None
            'risk_pct': float,     # % риска
            'reason': str,         # Пояснение
            'd1_fvg': bool,
            'h4_fvg': bool,
            'cot_ok': bool
        }
    """
    d1 = fvg_result.get("d1_active", False)
    h4 = fvg_result.get("h4_active", False)
    fvg_dir = fvg_result.get("direction")

    cot_signal = cot_verdict.get("signal", "neutral")
    cot_is_bearish = cot_signal in ("bearish", "strong_bearish")
    cot_is_bullish = cot_signal in ("bullish", "strong_bullish")
    cot_unknown = cot_signal in ("neutral", "N/A", None)

    # COT против = No trade
    if fvg_dir == "bullish" and cot_is_bearish:
        return _no_trade("COT медвежий — противоречит бычьему FVG")
    if fvg_dir == "bearish" and cot_is_bullish:
        return _no_trade("COT бычий — противоречит медвежьему FVG")

    # COT aligned or unknown
    cot_ok = not cot_is_bearish if fvg_dir == "bullish" else not cot_is_bullish if fvg_dir == "bearish" else False

    direction = "BUY" if fvg_dir == "bullish" else "SELL" if fvg_dir == "bearish" else None

    if not direction:
        return _no_trade("Нет направленного FVG")

    risk_pct = 0
    reason = ""

    if d1 and h4 and cot_ok:
        risk_pct = 2.0
        reason = "Полный сетап: D1 FVG + H4 FVG + COT aligned"
    elif d1 and h4 and cot_unknown:
        risk_pct = 1.5
        reason = "FVG D1+H4 есть, COT без сигнала — риск 1.5%"
    elif d1 and cot_ok and not h4:
        risk_pct = 1.0
        reason = "Только D1 FVG + COT — риск 1.0%"
    elif h4 and cot_ok and not d1:
        risk_pct = 1.0
        reason = "Только H4 FVG + COT — риск 1.0%"
    else:
        return _no_trade("Нет комбинации D1/H4 + COT")

    return {
        "trade": True,
        "direction": direction,
        "fvg_direction": fvg_dir,
        "risk_pct": risk_pct,
        "reason": reason,
        "d1_fvg": d1,
        "h4_fvg": h4,
        "cot_ok": cot_ok,
    }


def _no_trade(reason):
    return {
        "trade": False,
        "direction": None,
        "fvg_direction": None,
        "risk_pct": 0,
        "reason": reason,
        "d1_fvg": False,
        "h4_fvg": False,
        "cot_ok": False,
    }
