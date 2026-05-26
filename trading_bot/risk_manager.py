"""Risk Manager — расчёт размера позиции на основе фрактального SL."""

import config


def calculate_position(balance, entry_price, sl_price, risk_pct, pair):
    """
    Вычисляет размер позиции в юнитах.

    balance: баланс счёта
    entry_price: цена входа
    sl_price: уровень стоп-лосса
    risk_pct: % риска (например 2.0 = 2%)
    pair: ключ из config.PAIRS (для определения pip value)

    Returns: dict
    """
    sl_pips_abs = abs(entry_price - sl_price)
    risk_amount = balance * (risk_pct / 100)

    # Для валютных пар (EUR/USD, GBP/USD)
    # 1 pip = 0.0001, кроме JPY (0.01) и XAU (0.01 = $0.10 per 0.01 move)
    if "JPY" in pair:
        pip_size = 0.01
    elif "XAU" in pair:
        pip_size = 0.01  # 1 pip on XAU/USD = $0.10 per unit
    else:
        pip_size = 0.0001

    sl_pips = sl_pips_abs / pip_size

    if sl_pips == 0:
        return {"units": 0, "error": "SL равен цене входа — невозможно рассчитать"}

    # Pip value: для стандартного лота (100,000 units) = $10/pip
    # Для XAU: 1 unit двигается на $0.10 при изменении на 0.01
    if "XAU" in pair:
        units = risk_amount / (sl_pips_abs * 10)
    else:
        units = risk_amount / (sl_pips * 0.0001)

    return {
        "units": int(units),
        "sl_pips": round(sl_pips, 1),
        "risk_amount": round(risk_amount, 2),
        "sl_price": round(sl_price, 5),
        "tp_price": round(entry_price + (entry_price - sl_price) * config.RISK_RR, 5)
        if entry_price > sl_price
        else round(entry_price - (sl_price - entry_price) * config.RISK_RR, 5),
    }
