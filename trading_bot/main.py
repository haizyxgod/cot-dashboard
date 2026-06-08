"""COT + FVG Trading Bot v5 — ADX Adaptive TP + H1 Trigger.

Цепочка:
  1. FVG(D1+H4) задаёт направление
  2. Trend (EMA50/200 Gold, EMA10/30 Forex) валидирует
  3. COT (CFTC report) фильтрует: блокирует против, усиливает совпадение
  4. ADX: market regime -> adaptive TP (TREND=ATR, RANGE=1:1, NEUTRAL=default)
  5. H1 trigger: fractal breakout or volatility surge confirmation
  6. SL: H4 Fractal + ATR
  7. AUTO EXECUTE -> Telegram notification
  8. BE monitor: fractal breakout -> SL to entry
  9. Pyramiding: new entry when all positions at BE
"""

import sys
import os
import time
import threading
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cot_dashboard"))

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from mt5_client import client as mt5
from fvg_detector import check_fvg_signals
import db as database
from fractal_detector import nearest_fractal, calculate_atr, find_fractals
from signal_engine import evaluate_setup
from risk_manager import calculate_lot
from trend_filter import check_daily_trend
from entry_trigger import check_h1_trigger
import web_server
from web_server import add_log, bot_state
import config
import db as database

# Init DB
database.init()

# --- COT Engine ---
import cot_client
cot_client.init()
get_cot_verdict = cot_client.get_verdict  # alias for backward compat


last_signal_sent = {}

# BE tracking: ticket -> {entry_price, symbol, direction, be_triggered}
be_tracked = {}

# Pending signals waiting for H1 trigger
# Each: {pair_name, symbol, direction, entry_h4, sl, tp_price, risk_pct,
#         score, reason, sl_price, volume, rr, regime, signal_time, expires_at}
pending_signals = []
_pending_lock = threading.Lock()

import risk_protection
_last_healthcheck = datetime.now()


def _persist_pending():
    """Save pending signals to SQLite so they survive restarts."""
    import json
    serializable = []
    for ps in pending_signals:
        s = dict(ps)
        s["signal_time"] = s["signal_time"].isoformat()
        s["expires_at"] = s["expires_at"].isoformat()
        serializable.append(s)
    database.save_kv("pending_signals", json.dumps(serializable))


def _restore_pending():
    """Restore pending signals from SQLite after restart."""
    import json
    raw = database.load_kv("pending_signals")
    if not raw:
        return []
    try:
        data = json.loads(raw)
        restored = []
        now = datetime.now()
        for s in data:
            s["signal_time"] = datetime.fromisoformat(s["signal_time"])
            s["expires_at"] = datetime.fromisoformat(s["expires_at"])
            if now < s["expires_at"]:
                restored.append(s)
            else:
                print(f"  [PENDING] Expired on restore: {s['pair_name']} {s['direction']}")
        return restored
    except Exception as e:
        print(f"  [PENDING] Restore error: {e}")
        return []


def _calc_adx(df, period=14):
    """Calculate ADX(14) - trend strength indicator."""
    if len(df) < period + 1:
        return None
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr = np.zeros(len(high))
    plus_dm = np.zeros(len(high))
    minus_dm = np.zeros(len(high))

    for i in range(1, len(high)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        up = high[i] - high[i-1]
        down = low[i-1] - low[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0

    atr = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().values
    plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr
    minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 0.0001)
    adx = pd.Series(dx).ewm(alpha=1/period, adjust=False).mean().values

    return float(adx[-1])


def init_be_tracking():
    """Restore BE tracking for open positions after restart.
    Also log any positions that closed while the bot was down."""
    if not mt5.connect():
        return

    positions = mt5.get_positions()
    pos_by_ticket = {p["ticket"]: p for p in positions}

    # Load saved BE state from DB (persisted across restarts)
    saved_state = database.load_be_state()

    # Check saved positions: if closed during downtime, log them
    for ticket, info in saved_state.items():
        if ticket not in pos_by_ticket:
            # Position closed while bot was down — retroactively log
            print(f"[BE] Position #{ticket} {info['symbol']} closed during downtime — logging")
            _log_closed_trade(ticket, info)
            database.clear_be_ticket(ticket)
        else:
            # Restore from saved state (preserves be_triggered flag)
            be_tracked[ticket] = info
            tag = "BE" if info.get("be_triggered") else "tracking"
            print(f"[BE] Restored #{ticket} {info['symbol']} {info['direction']} ({tag})")

    # Also track open positions not yet in saved state (new or legacy)
    for p in positions:
        ticket = p["ticket"]
        if ticket in be_tracked:
            continue  # already restored above
        entry = p.get("price_open", p.get("open_price", 0))
        sl = float(p.get("sl", 0))
        direction = "BUY" if p["type"] == 0 else "SELL"
        symbol = p["symbol"]
        already_be = abs(sl - entry) < abs(entry) * 0.0001 if entry else False
        be_tracked[ticket] = {
            "symbol": symbol,
            "entry_price": entry,
            "direction": direction,
            "be_triggered": already_be,
        }
        tag = "BE" if already_be else "tracking"
        print(f"[BE] Restored #{ticket} {symbol} {direction} entry={entry} ({tag})")

    database.save_be_state(be_tracked)
    mt5.disconnect()
    print(f"[BE] Restored {len(be_tracked)} positions total")


def check_be():
    """Monitor open positions: capture live P&L, detect closes, check BE."""
    if not be_tracked:
        return
    if not mt5.connect():
        bot_state["mt5_connected"] = False
        return
    bot_state["mt5_connected"] = True

    positions = mt5.get_positions()
    pos_by_ticket = {p["ticket"]: p for p in positions}

    # Save live P&L for all tracked positions
    for p in positions:
        ticket = p["ticket"]
        if ticket in be_tracked:
            be_tracked[ticket]["last_profit"] = p.get("profit", 0)

    for ticket, info in list(be_tracked.items()):
        if ticket not in pos_by_ticket:
            # Position closed — use last known live profit
            _log_closed_trade(ticket, info)
            be_tracked.pop(ticket, None)
            database.clear_be_ticket(ticket)
            continue

        pos = pos_by_ticket[ticket]

        if info.get("be_triggered"):
            continue  # already moved to BE
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

    database.save_be_state(be_tracked)
    mt5.disconnect()


def _log_closed_trade(ticket, info):
    """When a position closes, determine result and save to DB."""
    try:
        from datetime import datetime
        entry_price = info["entry_price"]
        direction = info["direction"]
        symbol = info["symbol"]

        # Use last known live profit if available (more reliable than history)
        last_profit = info.get("last_profit", 0)
        if last_profit != 0:
            pnl = last_profit
            exit_price = 0
            volume = 0
            print(f"[LOG] #{ticket}: using live P&L = {pnl:.2f}")
        else:
            pnl, exit_price, volume = mt5.get_closed_trade_pnl(
                ticket, hours=72, symbol=symbol, entry_price=entry_price)
            print(f"[LOG] #{ticket}: history P&L = {pnl:.2f}")

        if exit_price == 0:
            exit_price = entry_price

        # Determine result
        if pnl > 0.01:
            result = "win"
        elif pnl < -0.01:
            result = "loss"
        else:
            result = "be"

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


def register_be(ticket, symbol, entry_price, direction, sl=0, tp=0, risk_pct=0):
    """Register a new position for BE tracking."""
    be_tracked[ticket] = {
        "symbol": symbol,
        "entry_price": entry_price,
        "direction": direction,
        "sl": sl,
        "tp": tp,
        "be_triggered": False,
        "risk_pct": risk_pct,
    }
    database.save_be_state(be_tracked)
    print(f"[BE] Tracking pos #{ticket} {symbol} {direction} entry={entry_price}")


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


def _healthcheck_ping():
    """Track last healthcheck timestamp for external monitoring."""
    global _last_healthcheck
    _last_healthcheck = datetime.now()


def scan_all(is_manual=False):
    """Главный цикл: FVG → Trend → COT → SL → сигнал."""
    print(f"\n[{datetime.now()}] === SCAN ===")

    # Manual mode: skip scheduled scans
    if not is_manual and not web_server.bot_state.get("auto_mode", True):
        print("  Auto mode OFF — skipping scheduled scan")
        return

    # Check risk limits
    ok, reason = risk_protection.check_limits(mt5, web_server, _tg)
    if not ok:
        print(f"  Limits blocked: {reason}")
        if reason in ("daily_loss",):
            web_server.add_log(f"Scan blocked: daily loss limit reached")
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
            msg = f"<span class='hl'>{pair_name}</span> — позиция без BE, пропуск"
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

            # Apply risk profile override
            risk_profile = web_server.bot_state.get("risk_profile", "challenge")
            profile = config.RISK_PROFILES.get(risk_profile, config.RISK_PROFILES["challenge"])
            score = signal.get("score", 2)
            base_risk = signal["risk_pct"]
            risk_pct = profile.get(score, base_risk)  # use profile override or signal default

            pos = calculate_lot(
                balance, entry, sl, risk_pct,
                pair_name, info["point"], info["trade_contract_size"],
                info["trade_tick_value"]
            )
            if pos.get("error"):
                msg = f"<span class='hl'>{pair_name}</span> — ошибка расчёта лота: {pos['error']}"
                print(f"  Risk err: {pos['error']}")
                web_server.add_log(msg)
                continue

            # --- ADX Adaptive TP ---
            strategy_mode = web_server.bot_state.get("strategy_mode", "adx_tp")
            is_forex = "JPY" in pair_name or "GBP" in pair_name

            d1_for_adx = df_d1_trend.tail(250)
            adx = _calc_adx(d1_for_adx, 14)

            if is_forex:
                trend_thresh, range_thresh = 20, 15
            else:
                trend_thresh, range_thresh = 25, 20

            if adx is None:
                regime = "neutral"
            elif adx > trend_thresh:
                regime = "trend"
            elif adx < range_thresh:
                regime = "range"
            else:
                regime = "neutral"

            if regime == "trend":
                tp_mult = config.TP_ATR_MULT_FOREX if is_forex else config.TP_ATR_MULT
                if atr_val and atr_val > 0:
                    if signal["direction"] == "BUY":
                        tp_price = entry + atr_val * tp_mult
                    else:
                        tp_price = entry - atr_val * tp_mult
                else:
                    tp_price = pos["tp_price"]
            elif regime == "range":
                sl_dist = abs(entry - sl)
                if signal["direction"] == "BUY":
                    tp_price = entry + sl_dist * 1.0
                else:
                    tp_price = entry - sl_dist * 1.0
            else:
                tp_price = pos["tp_price"]

            tp_price = round(tp_price, 5 if "JPY" in symbol else 2)
            print(f"  ADX={adx:.1f if adx else '?'} regime={regime} "
                  f"SL={sl:.5f} TP={tp_price:.5f}")

            # --- Max risk per idea check (pyramiding cap) ---
            current_risk = sum(
                be_tracked.get(p["ticket"], {}).get("risk_pct", 0)
                for p in open_positions if p["symbol"] == symbol
            )
            max_idea_risk = getattr(config, 'MAX_RISK_PER_IDEA_PCT', 3.0)
            if current_risk + risk_pct > max_idea_risk:
                risk_pct = max(0.25, max_idea_risk - current_risk)
                print(f"  Risk capped: {current_risk:.1f}% existing + new -> {risk_pct:.1f}% "
                      f"(max {max_idea_risk}%)")

            # --- Entry: immediate (adx_tp) or H1 trigger (adx_h1) ---
            if strategy_mode == "adx_tp":
                result = mt5.place_market_order(
                    symbol, signal["direction"],
                    float(pos["sl_price"]), float(tp_price),
                    float(pos["volume"])
                )
                if result is None:
                    print(f"  [AUTO] ORDER FAILED: {pair_name} {signal['direction']}")
                    _notify_error(pair_name, signal["direction"], "MT5 rejected order")
                    continue

                _execute_trade(
                    symbol, pair_name, signal["direction"], entry,
                    pos["sl_price"], tp_price, pos["volume"],
                    signal["risk_pct"], signal.get("reason", ""),
                    result.get("order", "?"),
                    extra_info=f" Strategy={strategy_mode}")

            else:
                # Pending — H1 trigger (adx_h1)
                expires_at = datetime.now() + timedelta(hours=config.TRIGGER_MAX_BARS)
                with _pending_lock:
                    pending_signals.append({
                        "pair_name": pair_name,
                        "symbol": symbol,
                        "direction": signal["direction"],
                        "entry_h4": entry,
                        "sl": sl,
                        "tp_price": tp_price,
                        "risk_pct": signal["risk_pct"],
                        "score": signal.get("score", 0),
                        "reason": signal["reason"],
                        "sl_price": pos["sl_price"],
                        "volume": pos["volume"],
                        "rr": pos.get("rr", 0),
                        "regime": regime,
                        "signal_time": datetime.now(),
                        "expires_at": expires_at,
                    })
                    _persist_pending()

                last_signal_sent[pair_name] = time.time()

                print(f"  [PENDING] {pair_name} {signal['direction']} "
                      f"Entry~{entry:.5f} SL={pos['sl_price']} TP={tp_price:.5f} "
                      f"Lot={pos['volume']} Regime={regime} "
                      f"Waiting H1 trigger (expires {expires_at.strftime('%H:%M')})")

                web_server.add_log(
                    f"<span class='hl'>{pair_name} {signal['direction']}</span> "
                    f"PENDING Entry~{entry:.5f} Lot={pos['volume']} "
                    f"Risk={signal['risk_pct']}% Score={signal.get('score',0)}/4 "
                    f"Regime={regime} | Waiting H1 trigger")

        except Exception as e:
            print(f"  [ERR] {pair_name}: {e}")
            import traceback; traceback.print_exc()

    print(f"[{datetime.now()}] === END ===\n")


def _execute_trade(symbol, pair_name, direction, entry, sl_price, tp_price,
                  volume, risk_pct, reason, order_id, extra_info=""):
    """Execute market order + register + save + notify. Shared by all strategies."""
    from datetime import datetime as dt

    # Find position_id from MT5
    position_id = None
    open_pos = mt5.get_positions()
    for p in open_pos:
        if p["symbol"] == symbol and abs(p["price_open"] - entry) < entry * 0.005:
            position_id = p["ticket"]
            break
    if position_id is None:
        candidates = [p for p in open_pos if p["symbol"] == symbol]
        if candidates:
            position_id = max(p["ticket"] for p in candidates)
    if position_id is None:
        position_id = order_id

    register_be(position_id, symbol, entry, direction, sl=sl_price, tp=tp_price, risk_pct=risk_pct)

    database.mark_trading_day()

    sid = database.save_signal({
        "pair": pair_name, "direction": direction,
        "entry_price": entry, "sl_price": sl_price,
        "tp_price": tp_price, "volume": volume,
        "risk_pct": risk_pct, "reason": reason + extra_info,
        "d1_fvg": 1, "h4_fvg": 1, "cot_text": "",
    })
    database.save_order(sid, {
        "pair": pair_name, "direction": direction,
        "entry_price": entry, "sl_price": sl_price,
        "tp_price": tp_price, "volume": volume,
    }, position_id)

    last_signal_sent[pair_name] = time.time()

    tag = "AUTO" if not extra_info else "PENDING"
    print(f"  [{tag}] #{order_id} (pos #{position_id}) {pair_name} "
          f"{direction} Entry={entry:.5f} SL={sl_price} "
          f"TP={tp_price} Lot={volume}{extra_info}")

    web_server.add_log(
        f"<span class='hl'>{pair_name} {direction}</span> "
        f"#{order_id} Entry={entry:.5f} Lot={volume} "
        f"Risk={risk_pct}% Score=?/4{extra_info}")

    _notify_trade(pair_name, direction, entry, sl_price, tp_price,
                  volume, risk_pct, reason + extra_info, order_id)


def cancel_pending_signals():
    """Cancel all pending signals. Called when switching away from H1 strategy."""
    global pending_signals
    with _pending_lock:
        n = len(pending_signals)
        pending_signals.clear()
        _persist_pending()
    if n:
        print(f"[STRATEGY] Cancelled {n} pending signal(s)")
        web_server.add_log(f"Cancelled {n} pending H1 signal(s) — strategy changed")


def check_pending_triggers():
    """Check pending signals for H1 trigger confirmation.
    Runs every 5 minutes. Enters trade when H1 fractal/volatility triggers,
    expires signals older than TRIGGER_MAX_BARS hours."""
    global pending_signals

    # Only active in H1 trigger mode
    if web_server.bot_state.get("strategy_mode", "adx_tp") != "adx_h1":
        return

    # Check risk limits
    ok, _ = risk_protection.check_limits(mt5, web_server, _tg)
    if not ok:
        return

    with _pending_lock:
        if not pending_signals:
            return

    if not mt5.connect():
        return

    try:
        still_pending = []
        now = datetime.now()

        with _pending_lock:
            signals = list(pending_signals)

        for ps in signals:
            # Check expiry
            if now > ps["expires_at"]:
                print(f"  [PENDING] EXPIRED: {ps['pair_name']} {ps['direction']} "
                      f"(signal at {ps['signal_time'].strftime('%H:%M')})")
                web_server.add_log(
                    f"<span class='hl'>{ps['pair_name']} {ps['direction']}</span> "
                    f"H1 trigger EXPIRED (no confirmation in 24h)")
                continue

            symbol = ps["symbol"]
            pair_name = ps["pair_name"]
            direction = ps["direction"]

            # Fetch H1 + H4 data for trigger check
            try:
                df_h1 = mt5.get_candles(symbol, "H1", 30)
                df_h4 = mt5.get_candles(symbol, "H4", 50)
            except Exception:
                still_pending.append(ps)
                continue

            if df_h1.empty or df_h4.empty:
                still_pending.append(ps)
                continue

            trigger = check_h1_trigger(df_h1, df_h4.tail(5), direction)

            if not trigger["triggered"]:
                still_pending.append(ps)
                continue

            # --- TRIGGERED! Execute trade ---
            tick = mt5.get_current_price(symbol)
            entry = tick["bid"] if direction == "SELL" else tick["ask"]

            acc = mt5.get_account_summary()
            info = mt5.get_symbol_info(symbol)
            if info is None:
                still_pending.append(ps)
                continue

            balance = float(acc.get("balance", 0))

            # Recalculate SL from ACTUAL entry price (not H4 signal price)
            atr_val = calculate_atr(df_h4, 14)
            sl = nearest_fractal(df_h4, direction.lower(), entry, atr_value=atr_val)
            if sl is None:
                print(f"  [PENDING] No fractal SL at trigger time for {pair_name}")
                still_pending.append(ps)
                continue

            # Recalculate TP from ACTUAL entry + SL with stored regime
            is_forex = "JPY" in pair_name or "GBP" in pair_name
            rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR

            # Apply risk profile override
            risk_profile = web_server.bot_state.get("risk_profile", "challenge")
            profile = config.RISK_PROFILES.get(risk_profile, config.RISK_PROFILES["challenge"])
            score = ps.get("score", 2)
            risk_pct = profile.get(score, ps["risk_pct"])

            pos = calculate_lot(
                balance, entry, sl, risk_pct,
                pair_name, info["point"], info["trade_contract_size"],
                info["trade_tick_value"], rr=rr
            )
            if pos.get("error"):
                print(f"  [PENDING] Lot recalc error: {pos['error']}")
                still_pending.append(ps)
                continue

            # Recalculate TP based on regime (same logic as scan_all)
            regime = ps["regime"]
            if regime == "trend":
                tp_mult = config.TP_ATR_MULT_FOREX if is_forex else config.TP_ATR_MULT
                if atr_val and atr_val > 0:
                    if direction == "BUY":
                        tp_price = entry + atr_val * tp_mult
                    else:
                        tp_price = entry - atr_val * tp_mult
                else:
                    tp_price = pos["tp_price"]
            elif regime == "range":
                sl_dist = abs(entry - sl)
                if direction == "BUY":
                    tp_price = entry + sl_dist * 1.0
                else:
                    tp_price = entry - sl_dist * 1.0
            else:
                tp_price = pos["tp_price"]

            tp_price = round(tp_price, 5 if "JPY" in symbol else 2)

            result = mt5.place_market_order(
                symbol, direction,
                float(pos["sl_price"]), float(tp_price),
                float(pos["volume"])
            )
            if result is None:
                print(f"  [PENDING] ORDER FAILED: {pair_name} {direction}")
                still_pending.append(ps)
                continue

            _execute_trade(
                symbol, pair_name, direction, entry,
                pos["sl_price"], tp_price, pos["volume"],
                ps["risk_pct"], ps["reason"],
                result.get("order", "?"),
                extra_info=f" | H1 {trigger['trigger_type']} Regime={regime}")

        with _pending_lock:
            pending_signals = still_pending
            _persist_pending()

    except Exception as e:
        print(f"  [PENDING] Error: {e}")
        import traceback; traceback.print_exc()
    finally:
        mt5.disconnect()


def _send_healthcheck():
    """Periodic status update to Telegram."""
    global _last_healthcheck
    _last_healthcheck = datetime.now()
    try:
        mt5.connect()
        acc = mt5.get_account_summary()
        positions = mt5.get_positions()
        mt5.disconnect()

        balance = acc.get("balance", 0)
        equity = acc.get("equity", 0)
        open_count = len(positions)
        open_pnl = sum(p.get("profit", 0) for p in positions)

        orders = database.get_order_history(5000)
        closed = [o for o in orders if o.get("result") in ("win", "loss", "be")]
        today = datetime.now().strftime("%Y-%m-%d")
        daily_pnl = sum(o.get("pnl", 0) for o in closed if (o.get("time") or "").startswith(today))
        wins = sum(1 for o in closed if o.get("result") == "win")
        losses = sum(1 for o in closed if o.get("result") == "loss")
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        with _pending_lock:
            pending_n = len(pending_signals)
        mt5_icon = "✅" if bot_state.get("mt5_connected") else "❌"
        dd_pct = (risk_protection.peak_balance - equity) / risk_protection.peak_balance * 100 if risk_protection.peak_balance > 0 else 0
        dd_line = f"Просадка: *-{dd_pct:.1f}%* (пик ${risk_protection.peak_balance:,.0f})\n" if dd_pct > 0 else ""
        dstop = " [DAILY STOP]" if risk_protection.daily_stopped else ""
        pstop = " [PAUSED]" if risk_protection.bot_paused else ""
        pending_line = f"Ожидают H1: *{pending_n}* сигн.\n" if pending_n else ""
        _tg(
            f"🤖 *Бот жив*{dstop}{pstop} | {datetime.now().strftime('%H:%M')}\n"
            f"Баланс: *${balance:,.0f}* | Equity: *${equity:,.0f}*\n"
            f"Открыто: *{open_count}* поз. | P&L: *${open_pnl:+,.0f}*\n"
            f"{dd_line}"
            f"{pending_line}"
            f"Сегодня: *${daily_pnl:+,.0f}* | Win Rate: *{wr:.1f}%*\n"
            f"MT5: {mt5_icon} | Сделок: {len(closed)}"
        )
    except Exception as e:
        _tg(f"⚠️ *Healthcheck error:* {e}")


sched = None  # module-level for web resume access

if __name__ == "__main__":
    print("=" * 50)
    print("COT + FVG Bot v5 — ADX Adaptive TP + H1 Trigger")
    print(f"Server: {config.MT5_SERVER}")
    print(f"Pairs: {list(config.PAIRS.keys())}")
    print(f"UI: http://localhost:5002/bot (monitoring)")
    print("=" * 50)

    web_server.start_web()

    def rapid_pnl_tracker():
        """Every 15 sec: capture live P&L + detect closed positions."""
        check_be()

    sched = BackgroundScheduler()
    sched.add_job(scan_all, CronTrigger(hour="*/3", minute=0),
                  id="scan")
    sched.add_job(check_be, "interval", seconds=config.TRIGGER_SCAN_SEC,
                  id="be_monitor")
    sched.add_job(check_pending_triggers, "interval", seconds=config.TRIGGER_SCAN_SEC,
                  id="h1_trigger_monitor")
    sched.add_job(rapid_pnl_tracker, "interval", seconds=15,
                  id="rapid_pnl")
    # Healthcheck at 9:00, 15:00, 21:00
    sched.add_job(_send_healthcheck, CronTrigger(hour="9,15,21", minute=17),
                  id="healthcheck")
    sched.start()
    print("[OK] Scan: at 0:00, 3:00, 6:00, 9:00, 12:00, 15:00, 18:00, 21:00")
    print(f"[OK] BE monitor: every {config.TRIGGER_SCAN_SEC}s")
    print("[OK] H1 trigger monitor: every 5min")
    print("[OK] Rapid P&L tracker: every 15s")
    print("[OK] Healthcheck: at 9:17, 15:17, 21:17")

    # Telegram polling (daemon thread)
    from telegram_bot import start_polling
    threading.Thread(target=start_polling, daemon=True, name="tg-polling").start()
    print("[OK] Telegram polling started")

    # Send startup message (non-blocking — Telegram may not be ready yet)
    def _send_startup_msg():
        try:
            from telegram_bot import send_text
            send_text("🤖 *Бот запущен* (v5.1) — клавиатура активна.")
        except Exception as e:
            print(f"[TG] Startup message failed: {e}")
    threading.Thread(target=_send_startup_msg, daemon=True).start()

    print("\n[Init] Restoring BE state...")
    init_be_tracking()

    print("\n[Init] Restoring pending signals...")
    restored = _restore_pending()
    if restored:
        pending_signals = restored
        print(f"  Restored {len(restored)} pending signal(s)")
    else:
        print("  No pending signals to restore")

    # Init risk limits
    try:
        mt5.connect()
        acc = mt5.get_account_summary()
        mt5.disconnect()
        bal = float(acc.get("balance", 0))
        risk_protection.init_limits(bal)
        print(f"[Init] Risk limits: day_start=${bal:,.0f} peak=${bal:,.0f}")
    except Exception as e:
        print(f"[Init] Risk init error: {e}")

    print("\n[Init] First scan...")
    scan_all()

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        mt5.disconnect()
        sched.shutdown()
        print("\n[OK] Stopped")
