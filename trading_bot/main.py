"""COT + FVG Trading Bot v4 — AUTO EXECUTION.

Цепочка:
  1. FVG(D1+H4) задаёт направление
  2. Trend (EMA50/200 Gold, EMA10/30 Forex) валидирует
  3. COT (CFTC report) фильтрует: блокирует против, усиливает совпадение
  4. ADX: market regime → adaptive TP (TREND=ATR, RANGE=1:1, NEUTRAL=default)
  5. SL: H4 Fractal + ATR
  6. AUTO EXECUTE → Telegram notification
  7. BE monitor: fractal breakout → SL to entry
  8. Pyramiding: new entry when all positions at BE
"""

import sys
import os
import time
import threading
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
from web_server import add_log, bot_state
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
    mapping = {"XAU/USD": "XAU (Золото)", "EUR/USD": "EUR/USD",
               "GBP/USD": "GBP/USD", "USD/JPY": "USD/JPY"}
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

        signal = v.get("signal", "neutral")
        direction = analysis.get("sentiment", {}).get("direction", "neutral")

        # JPY COT inversion: COT tracks JPY futures → flip direction
        if "JPY" in pair_name:
            inv = {"bullish": "bearish", "bearish": "bullish",
                   "strong_bullish": "strong_bearish",
                   "strong_bearish": "strong_bullish"}
            signal = inv.get(signal, signal)
            direction = inv.get(direction, direction)

        return {
            "signal": signal,
            "score": v.get("score", 0),
            "direction": direction,
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
        bot_state["mt5_connected"] = False
        return
    bot_state["mt5_connected"] = True

    positions = mt5.get_positions()
    pos_by_ticket = {p["ticket"]: p for p in positions}

    for ticket, info in list(be_tracked.items()):
        if info.get("be_triggered"):
            continue  # already moved to BE

        if ticket not in pos_by_ticket:
            # Position closed — log to DB
            _log_closed_trade(ticket, info)
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
                    web_server.add_log(
                        f"<span class='hl'>{symbol}</span> #{ticket} "
                        f"SL → BE ({info['entry_price']})")
                    _notify_be(ticket, symbol, direction, info["entry_price"])
                else:
                    print(f"[BE] Ticket #{ticket}: modify SL failed")
        except Exception as e:
            print(f"[BE] Error ticket #{ticket}: {e}")

    mt5.disconnect()


def _log_closed_trade(ticket, info):
    """When a position closes, determine result and save to DB."""
    try:
        from datetime import datetime
        deals = mt5.get_position_history(hours=48)
        entry_price = info["entry_price"]
        direction = info["direction"]
        symbol = info["symbol"]
        pnl = 0
        exit_price = 0
        volume = 0

        # Find matching deal
        for d in deals:
            if d.get("position_id") == ticket or d.get("order") == ticket:
                pnl += d.get("profit", 0)
                if d.get("price") and d.get("price") != entry_price:
                    exit_price = d["price"]
                volume = d.get("volume", volume)

        if exit_price == 0:
            exit_price = entry_price

        # Determine result
        if pnl > 0.01:
            result = "win"
        elif pnl < -0.01:
            result = "loss"
        else:
            result = "be"

        import db as database
        database.save_closed_trade(
            ticket=ticket, pair=symbol_to_pair(symbol),
            direction=direction, entry_price=entry_price,
            sl_price=info.get("sl", 0), tp_price=info.get("tp", 0),
            volume=volume, pnl=pnl, result=result,
            exit_price=exit_price,
            open_time=str(datetime.now())
        )
        print(f"[LOG] #{ticket} {symbol} {direction}: {result} PnL=${pnl:.2f}")
        _notify_close(ticket, symbol, direction, pnl, result, exit_price)
        web_server.add_log(
            f"<span class='hl'>{symbol}</span> #{ticket}: "
            f"{result.upper()} ${pnl:+.2f}")
    except Exception as e:
        print(f"[LOG] Error logging #{ticket}: {e}")


def symbol_to_pair(symbol):
    for pair_name, sym in config.PAIRS.items():
        if sym == symbol:
            return pair_name
    return symbol


def register_be(ticket, symbol, entry_price, direction):
    """Register a new position for BE tracking."""
    be_tracked[ticket] = {
        "symbol": symbol,
        "entry_price": entry_price,
        "direction": direction,
        "be_triggered": False,
    }
    print(f"[BE] Tracking #{ticket} {symbol} {direction} entry={entry_price}")


def _tg(msg):
    """Send Telegram message, fail silently."""
    try:
        from telegram_bot import send_text
        send_text(msg)
    except Exception as e:
        print(f"[TG] Error: {e}")


def _notify_trade(pair, direction, entry, sl, tp, vol, risk_pct, reason, order_id):
    emoji = "\U0001f7e2" if direction == "BUY" else "\U0001f534"
    _tg(f"{emoji} *{pair} {direction}* #{order_id}\n"
        f"Entry: {entry:.5f} | Lot: {vol}\n"
        f"SL: {sl:.5f} | TP: {tp:.5f}\n"
        f"Risk: {risk_pct}% | {reason}")


def _notify_close(ticket, symbol, direction, pnl, result, exit_price):
    if result == "win":
        emoji, label = "✅", "TP HIT"
    elif result == "loss":
        emoji, label = "❌", "SL HIT"
    else:
        emoji, label = "➖", "BE"
    _tg(f"{emoji} *{symbol} {direction}* #{ticket} — {label}\n"
        f"Exit: {exit_price:.5f} | PnL: ${pnl:+.2f}")


def _notify_be(ticket, symbol, direction, entry):
    _tg(f"\U0001f7e1 *{symbol} {direction}* #{ticket}\n"
        f"SL -> BE ({entry:.5f})")


def _notify_error(pair, direction, reason):
    _tg(f"⚠️ *{pair} {direction}* — ORDER FAILED\n{reason}")


def scan_all(is_manual=False):
    """Главный цикл: FVG → Trend → COT → SL → сигнал."""
    print(f"\n[{datetime.now()}] === SCAN ===")

    # Manual mode: skip scheduled scans
    if not is_manual and not web_server.bot_state.get("auto_mode", True):
        print("  Auto mode OFF — skipping scheduled scan")
        return

    if not mt5.connect():
        print("  MT5 not connected (reconnecting...)")
        bot_state["mt5_connected"] = False
        web_server.add_log("MT5 connection lost — reconnecting...")
        return

    bot_state["mt5_connected"] = True
    bot_state["last_scan"] = datetime.now().isoformat()
    web_server.add_log("Сканирование начато")

    open_positions = mt5.get_positions()
    occupied_symbols = {p["symbol"] for p in open_positions}

    # Pyramiding: allow new entry if all positions on this symbol are at BE
    def can_add_position(symbol):
        symbol_positions = [p for p in open_positions if p["symbol"] == symbol]
        if not symbol_positions:
            return True
        if len(symbol_positions) >= getattr(config, 'MAX_POSITIONS_PER_PAIR', 2):
            return False
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
            msg = f"<span class='hl'>{pair_name}</span> — позиция без БУ, пропуск"
            print(f"  [{pair_name}] Position without BE — skip")
            web_server.add_log(msg)
            continue

        last_ts = last_signal_sent.get(pair_name, 0)
        if (now_ts - last_ts) < cooldown_sec:
            msg = f"<span class='hl'>{pair_name}</span> — кулдаун ({int((now_ts - last_ts)/3600)}h), пропуск"
            print(f"  [{pair_name}] Cooldown ({int((now_ts - last_ts)/3600)}h) — skip")
            web_server.add_log(msg)
            continue

        try:
            print(f"\n--- {pair_name} ({symbol}) ---")

            df_d1 = mt5.get_candles(symbol, "D", 20)
            df_d1_trend = mt5.get_candles(symbol, "D", 250)
            df_h4 = mt5.get_candles(symbol, "H4", 50)

            if df_d1.empty or df_h4.empty:
                msg = f"<span class='hl'>{pair_name}</span> — нет данных свечей"
                print("  No data")
                web_server.add_log(msg)
                continue

            # Stage 1: FVG direction
            fvg = check_fvg_signals(df_d1, df_h4)
            print(f"  FVG: D1={fvg['d1_active']} H4={fvg['h4_active']} dir={fvg['direction']}")

            if not fvg["direction"]:
                msg = (f"<span class='hl'>{pair_name}</span> — нет FVG "
                       f"(D1={'✓' if fvg['d1_active'] else '✗'} H4={'✓' if fvg['h4_active'] else '✗'})")
                web_server.add_log(msg)
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
                msg = (f"<span class='hl'>{pair_name}</span> — сигнал отклонён: "
                       f"{signal['reason']} | FVG={fvg['direction']} COT={cot['signal']} Trend={trend}")
                print(f"  -> {signal['reason']}")
                web_server.add_log(msg)
                continue

            print(f"  -> {signal['direction']} | score={signal.get('score', 0)}/4 "
                  f"| risk={signal['risk_pct']}%")

            tick = mt5.get_current_price(symbol)
            entry = tick["bid"] if signal["direction"] == "SELL" else tick["ask"]

            atr_val = calculate_atr(df_h4, 14)
            sl = nearest_fractal(df_h4, fvg["direction"], entry, atr_value=atr_val)
            if sl is None:
                msg = f"<span class='hl'>{pair_name}</span> — нет фрактала для SL"
                print("  No fractal SL")
                web_server.add_log(msg)
                continue

            acc = mt5.get_account_summary()
            info = mt5.get_symbol_info(symbol)
            if info is None:
                msg = f"<span class='hl'>{pair_name}</span> — нет данных символа MT5"
                print(f"  No symbol info for {symbol}")
                web_server.add_log(msg)
                continue

            balance = float(acc.get("balance", 0))
            pos = calculate_lot(
                balance, entry, sl, signal["risk_pct"],
                pair_name, info["point"], info["trade_contract_size"],
                info["trade_tick_value"]
            )
            if pos.get("error"):
                msg = f"<span class='hl'>{pair_name}</span> — ошибка расчёта лота: {pos['error']}"
                print(f"  Risk err: {pos['error']}")
                web_server.add_log(msg)
                continue

            # --- AUTO EXECUTE ---
            result = mt5.place_market_order(
                symbol, signal["direction"],
                float(pos["sl_price"]), float(pos["tp_price"]),
                float(pos["volume"])
            )

            if result is None:
                print(f"  [AUTO] ORDER FAILED: {pair_name} {signal['direction']}")
                _notify_error(pair_name, signal["direction"],
                              f"MT5 rejected order")
                continue

            order_id = result.get("order", "?")
            register_be(order_id, symbol, entry, signal["direction"])
            last_signal_sent[pair_name] = time.time()

            print(f"  [AUTO] #{order_id} {pair_name} {signal['direction']} "
                  f"Entry={entry:.5f} SL={pos['sl_price']} TP={pos['tp_price']} "
                  f"Lot={pos['volume']}")

            web_server.add_log(
                f"<span class='hl'>{pair_name} {signal['direction']}</span> "
                f"#{order_id} Entry={entry:.5f} Lot={pos['volume']} "
                f"Risk={signal['risk_pct']}% Score={signal.get('score',0)}/4")

            # Telegram notification
            _notify_trade(pair_name, signal["direction"], entry,
                          pos["sl_price"], pos["tp_price"],
                          pos["volume"], signal["risk_pct"],
                          signal["reason"], order_id)

        except Exception as e:
            print(f"  [ERR] {pair_name}: {e}")
            import traceback; traceback.print_exc()

    print(f"[{datetime.now()}] === END ===\n")


if __name__ == "__main__":
    print("=" * 50)
    print("COT + FVG Bot v4 — AUTO MODE")
    print(f"Server: {config.MT5_SERVER}")
    print(f"Pairs: {list(config.PAIRS.keys())}")
    print(f"UI: http://localhost:5002/bot (monitoring)")
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

    # Telegram polling (daemon thread)
    from telegram_bot import start_polling
    threading.Thread(target=start_polling, daemon=True, name="tg-polling").start()
    print("[OK] Telegram polling started")

    # Send startup message with keyboard
    sys.stdout.flush()
    try:
        from telegram_bot import send_text
        send_text("🤖 *Бот запущен* — клавиатура активна.")
    except Exception as e:
        print(f"[TG] Startup message failed: {e}")

    print("\n[Init] First scan...")
    scan_all()

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        mt5.disconnect()
        sched.shutdown()
        print("\n[OK] Stopped")
