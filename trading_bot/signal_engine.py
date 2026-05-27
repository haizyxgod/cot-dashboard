"""Signal Engine v3 — FVG-first + COT as filter + Trend validation.

FVG(D1+H4) задаёт направление. COT и Trend — фильтры:
  - COT против FVG → NO TRADE
  - Trend против FVG → NO TRADE
  - COT aligned → +1 к score
  - Trend aligned → +1 к score

Score 0-4 (D1 FVG + H4 FVG + COT aligned + Trend aligned):
  3-4 → 2.0% | 2 → 1.0% | 1 → 0.5% | 0 → no trade
"""


def evaluate_setup(fvg_result, cot_verdict, trend=None):
    """
    FVG-first decision matrix with COT + Trend filters.

    fvg_result: dict from fvg_detector.check_fvg_signals()
    cot_verdict: dict with keys: signal, score, direction
    trend: 'bullish' | 'bearish' | 'neutral' | None
    """
    d1 = fvg_result.get("d1_active", False)
    h4 = fvg_result.get("h4_active", False)
    fvg_dir = fvg_result.get("direction")

    if not fvg_dir:
        return _no_trade("Нет направленного FVG")

    # --- COT filter ---
    cot_signal = cot_verdict.get("signal", "neutral")
    cot_is_bearish = cot_signal in ("bearish", "strong_bearish")
    cot_is_bullish = cot_signal in ("bullish", "strong_bullish")
    cot_unknown = cot_signal in ("neutral", "N/A", None)
    cot_aligned = (
        (fvg_dir == "bullish" and cot_is_bullish)
        or (fvg_dir == "bearish" and cot_is_bearish)
    )
    cot_opposed = (
        (fvg_dir == "bullish" and cot_is_bearish)
        or (fvg_dir == "bearish" and cot_is_bullish)
    )

    if cot_opposed:
        return _no_trade(
            f"COT ({cot_signal}) противоречит FVG ({fvg_dir})", "cot_opposed"
        )

    # --- Trend filter ---
    trend_is_set = trend is not None and trend != "neutral"
    trend_opposes = trend_is_set and (
        (fvg_dir == "bullish" and trend == "bearish")
        or (fvg_dir == "bearish" and trend == "bullish")
    )
    trend_aligns = trend_is_set and not trend_opposes

    if trend_opposes:
        return _no_trade(
            f"Trend ({trend}) противоречит FVG ({fvg_dir})", "trend_opposed"
        )

    # --- Direction ---
    direction = "BUY" if fvg_dir == "bullish" else "SELL"

    # --- Scoring (0-4) ---
    score = 0
    if d1:
        score += 1
    if h4:
        score += 1
    if cot_aligned:
        score += 1
    if trend_aligns:
        score += 1

    parts = [
        f"D1={'Y' if d1 else 'N'}",
        f"H4={'Y' if h4 else 'N'}",
        f"COT={'Y' if cot_aligned else '?' if cot_unknown else 'N'}",
        f"Trend={'Y' if trend_aligns else '?' if not trend_is_set else 'N'}",
    ]
    detail = " | ".join(parts)

    if score >= 3:
        risk_pct = 2.0
        reason = f"Полный сетап ({score}/4): {detail}"
    elif score == 2:
        risk_pct = 1.0
        reason = f"Средний сигнал ({score}/4): {detail}"
    elif score == 1:
        risk_pct = 0.5
        reason = f"Слабый сигнал ({score}/4): {detail}"
    else:
        return _no_trade(f"Нет переменных ({score}/4)")

    return {
        "trade": True,
        "direction": direction,
        "fvg_direction": fvg_dir,
        "risk_pct": risk_pct,
        "reason": reason,
        "d1_fvg": d1,
        "h4_fvg": h4,
        "cot_ok": not cot_opposed,
        "cot_aligned": cot_aligned,
        "trend_ok": trend_aligns,
        "trend": trend,
        "score": score,
    }


def _no_trade(reason, block_reason="unknown"):
    return {
        "trade": False,
        "direction": None,
        "fvg_direction": None,
        "risk_pct": 0,
        "reason": reason,
        "d1_fvg": False,
        "h4_fvg": False,
        "cot_ok": False,
        "cot_aligned": False,
        "trend_ok": None,
        "trend": None,
        "score": 0,
        "state": block_reason,
    }
