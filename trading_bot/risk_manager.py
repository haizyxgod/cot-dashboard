"""Risk Manager — pair-aware lot sizing (MT5)."""
import config


def calculate_lot(balance, entry_price, sl_price, risk_pct, pair,
                 point, contract_size, tick_value=None, rr=None):
    """
    point: symbol_info.point
    contract_size: symbol_info.trade_contract_size
    tick_value: symbol_info.trade_tick_value
    rr: Risk:Reward (default: pair-specific from config)
    """
    if rr is None:
        rr = config.RISK_RR_FOREX if _is_forex(pair) else config.RISK_RR

    sl_distance = abs(entry_price - sl_price)
    if sl_distance == 0:
        return {"volume": 0, "error": "SL equals entry"}

    risk_amount = balance * (risk_pct / 100)
    sl_points = sl_distance / point

    if tick_value and tick_value > 0:
        sl_value_per_lot = sl_points * tick_value
    else:
        sl_value_per_lot = sl_distance * contract_size

    if sl_value_per_lot <= 0:
        return {"volume": 0, "error": "Zero SL value"}

    volume = risk_amount / sl_value_per_lot
    volume = round(volume, 2)
    volume = max(0.01, volume)

    tp_distance = sl_distance * rr
    if entry_price > sl_price:
        tp_price = entry_price + tp_distance
    else:
        tp_price = entry_price - tp_distance

    return {
        "volume": volume,
        "sl_points": round(sl_points, 1),
        "risk_amount": round(risk_amount, 2),
        "sl_price": round(sl_price, 5),
        "tp_price": round(tp_price, 5),
        "rr": rr,
    }


def _is_forex(pair):
    return pair in ("USD/JPY", "GBP/USD")
