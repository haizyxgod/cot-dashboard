"""COT + FVG Trading Bot v3 — FVG-first + COT filter + Trend + ATR SL.

Цепочка:
  1. FVG(D1+H4) задаёт направление
  2. Trend (EMA50/200) валидирует
  3. COT (CFTC report) фильтрует: блокирует против, усиливает совпадение
  4. SL: H4 Fractal + ATR
  5. Risk: 0.5% / 1% / 2% по числу совпавших переменных
"""

import sys
import os
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cot_dashboard"))

from apscheduler.schedulers.background import BackgroundScheduler
from mt5_client import client as mt5
from fvg_detector import check_fvg_signals
from fractal_detector import nearest_fractal, calculate_atr, find_fractals
from signal_engine import evaluate_setup
from risk_manager import calculate_lot
from trend_filter import check_daily_trend
import web_server
import config
import db as database

# Init DB
database.init()

# --- COT Engine ---
try:
    from cot_fetcher import COTDataFetcher
    cot_fetcher = COTDataFetcher()
    COT_AVAILABLE = True
    print("[OK] COT engine loaded")
except ImportError:
    COT_AVAILABLE = False
    print("[WARN] COT not available")


def get_cot_verdict(pair_name):
    if not COT_AVAILABLE:
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "COT недоступен"}
    mapping = {"XAU/USD": "XAU (Золото)", "EUR/USD": "EUR/USD", "GBP/USD": "GBP/USD"}
    cot_key = mapping.get(pair_name)
    if not cot_key:
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Нет данных"}
    try:
        data = cot_fetcher.fetch_latest_data(cot_key, limit=2)
        if not data or len(data) < 2:
            return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Нет данных"}
        analysis = cot_fetcher.advanced_analysis(cot_key, data)
        if not analysis:
            return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Ошибка"}
        v = analysis.get("verdict", {})
        return {
            "signal": v.get("signal", "neutral"),
            "score": v.get("score", 0),
            "direction": analysis.get("sentiment", {}).get("direction", "neutral"),
            "text": v.get("text", "N/A"),
        }
    except Exception as e:
        print(f"COT error {pair_name}: {e}")
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Ошибка"}


last_signal_sent = {}

# BE tracking: ticket -> {entry_price, symbol, direction, be_triggered}
be_tracked = {}


def check_be():
    """Monitor open positions for H4 fractal breakout → move SL to entry."""
    if not be_tracked:
        return
    if not mt5.connect():
        return

    positions = mt5.get_positions()
    pos_by_ticket = {p["ticket"]: p for p in positions}

    for ticket, info in list(be_tracked.items()):
        if info.get("be_triggered"):
            continue  # already moved to BE

        if ticket not in pos_by_ticket:
            # Position closed — clean up
            be_tracked.pop(ticket, None)
            continue

        pos = pos_by_ticket[ticket]
        symbol = info["symbol"]
        entry = info["entry_price"]
        direction = info["direction"]

        try:
            df_h4 = mt5.get_candles(symbol, "H4", 50)
            if df_h4.empty:
                continue

            df_f = find_fractals(df_h4)
            recent = df_f.tail(5)

            fractal_crossed = False
            if direction == "BUY":
                ups = recent[recent["up_fractal"].notna()]
                if not ups.empty and ups["up_fractal"].iloc[-1] > entry:
                    fractal_crossed = True
            else:  # SELL
                downs = recent[recent["down_fractal"].notna()]
                if not downs.empty and downs["down_fractal"].iloc[-1] < entry:
                    fractal_crossed = True

            if fractal_crossed:
                # Modify SL to entry
                current_sl = pos.get("sl", 0)
                if abs(current_sl - entry) < 0.0001:
                    # Already at BE
                    info["be_triggered"] = True
                    continue

                ok = mt5.modify_sl(ticket, entry)
                if ok:
                    info["be_triggered"] = True
                    print(f"[BE] Ticket #{ticket} {symbol} {direction}: "
                          f"SL moved to {entry} (BE)")
                else:
                    print(f"[BE] Ticket #{ticket}: modify SL failed")
        except Exception as e:
            print(f"[BE] Error ticket #{ticket}: {e}")

    mt5.disconnect()


def register_be(ticket, symbol, entry_price, direction):
    """Register a new position for BE tracking."""
    be_tracked[ticket] = {
        "symbol": symbol,
        "entry_price": entry_price,
        "direction": direction,
        "be_triggered": False,
    }
    print(f"[BE] Tracking #{ticket} {symbol} {direction} entry={entry_price}")


def scan_all():
    """Главный цикл: FVG → Trend → COT → SL → сигнал."""
    print(f"\n[{datetime.now()}] === SCAN ===")

    if not mt5.connect():
        print("  MT5 not connected")
        return

    web_server.pending_signals.clear()

    open_positions = mt5.get_positions()
    occupied_symbols = {p["symbol"] for p in open_positions}

    # Pyramiding: allow new entry if all positions on this symbol are at BE
    def can_add_position(symbol):
        symbol_positions = [p for p in open_positions if p["symbol"] == symbol]
        if not symbol_positions:
            return True
        for p in symbol_positions:
            ticket = p["ticket"]
            info = be_tracked.get(ticket, {})
            if not info.get("be_triggered"):
                return False  # at least one position without BE
        return True  # all positions at BE — safe to add

    now_ts = time.time()
    cooldown_sec = config.MIN_BARS_BETWEEN_TRADES * 4 * 3600

    for pair_name, symbol in config.PAIRS.items():

        if symbol in occupied_symbols and not can_add_position(symbol):
            print(f"  [{pair_name}] Position without BE — skip")
            continue

        last_ts = last_signal_sent.get(pair_name, 0)
        if (now_ts - last_ts) < cooldown_sec:
            print(f"  [{pair_name}] Cooldown ({int((now_ts - last_ts)/3600)}h) — skip")
            continue

        try:
            print(f"\n--- {pair_name} ({symbol}) ---")

            df_d1 = mt5.get_candles(symbol, "D", 20)
            df_d1_trend = mt5.get_candles(symbol, "D", 250)
            df_h4 = mt5.get_candles(symbol, "H4", 50)

            if df_d1.empty or df_h4.empty:
                print("  No data")
                continue

            # Stage 1: FVG direction
            fvg = check_fvg_signals(df_d1, df_h4)
            print(f"  FVG: D1={fvg['d1_active']} H4={fvg['h4_active']} dir={fvg['direction']}")

            if not fvg["direction"]:
                continue

            # Stage 2: COT filter
            cot = get_cot_verdict(pair_name)
            print(f"  COT: {cot['signal']} ({cot.get('text', '')})")

            # Stage 3: Trend
            trend = check_daily_trend(df_d1_trend, pair_name)
            print(f"  Trend: {trend}")

            # Evaluate (FVG-first, COT+Trend as filters)
            signal = evaluate_setup(fvg, cot, trend)
            if not signal["trade"]:
                print(f"  -> {signal['reason']}")
                continue

            print(f"  -> {signal['direction']} | score={signal.get('score', 0)}/4 "
                  f"| risk={signal['risk_pct']}%")

            tick = mt5.get_current_price(symbol)
            entry = tick["bid"] if signal["direction"] == "SELL" else tick["ask"]

            atr_val = calculate_atr(df_h4, 14)
            sl = nearest_fractal(df_h4, fvg["direction"], entry, atr_value=atr_val)
            if sl is None:
                print("  No fractal SL")
                continue

            acc = mt5.get_account_summary()
            info = mt5.get_symbol_info(symbol)
            if info is None:
                print(f"  No symbol info for {symbol}")
                continue

            balance = float(acc.get("balance", 0))
            pos = calculate_lot(
                balance, entry, sl, signal["risk_pct"],
                pair_name, info["point"], info["trade_contract_size"],
                info["trade_tick_value"]
            )
            if pos.get("error"):
                print(f"  Risk err: {pos['error']}")
                continue

            sig_id = int(time.time() * 1000)
            sig_data = {
                "pair": pair_name,
                "direction": signal["direction"],
                "d1_fvg": signal["d1_fvg"],
                "h4_fvg": signal["h4_fvg"],
                "cot_text": cot.get("text", "N/A"),
                "entry_price": entry,
                "sl_price": pos["sl_price"],
                "tp_price": pos["tp_price"],
                "volume": pos["volume"],
                "risk_pct": signal["risk_pct"],
                "reason": signal["reason"],
                "time": str(datetime.now()),
            }
            web_server.pending_signals[sig_id] = sig_data
            last_signal_sent[pair_name] = time.time()
            print(f"  [WEB] Signal #{sig_id}: {sig_data['pair']} {sig_data['direction']} "
                  f"vol={sig_data['volume']}")

        except Exception as e:
            print(f"  [ERR] {pair_name}: {e}")
            import traceback; traceback.print_exc()

    print(f"[{datetime.now()}] === END ({len(web_server.pending_signals)} signals) ===\n")


if __name__ == "__main__":
    print("=" * 50)
    print("COT + FVG Bot v3 (FVG-first + COT filter)")
    print(f"Server: {config.MT5_SERVER}")
    print(f"Pairs: {list(config.PAIRS.keys())}")
    print(f"UI: http://localhost:5002/bot")
    print("=" * 50)

    web_server.start_web()

    sched = BackgroundScheduler()
    sched.add_job(scan_all, "interval", minutes=config.SCAN_INTERVAL_MINUTES,
                  id="scan")
    sched.add_job(check_be, "interval", seconds=config.TRIGGER_SCAN_SEC,
                  id="be_monitor")
    sched.start()
    print(f"[OK] Scan: every {config.SCAN_INTERVAL_MINUTES} min")
    print(f"[OK] BE monitor: every {config.TRIGGER_SCAN_SEC}s")

    print("\n[Init] First scan...")
    scan_all()

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        mt5.disconnect()
        sched.shutdown()
        print("\n[OK] Stopped")
