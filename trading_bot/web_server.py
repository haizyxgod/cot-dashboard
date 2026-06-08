"""Web Dashboard v3 — equity graph, monthly P&L, CSV export, theme, mobile."""
import threading
import io
import csv
from datetime import datetime
from flask import Flask, render_template, render_template_string, jsonify, request, Response
from flask_cors import CORS
import db as database

app = Flask(__name__)
CORS(app, origins=["http://127.0.0.1:5002", "http://localhost:5002"])

pending_signals = {}
database.init()

bot_state = {"last_scan": None, "scan_result": "", "mt5_connected": False, "auto_mode": True,
             "strategy_mode": "adx_tp", "risk_profile": "challenge"}
log_entries = []

def add_log(msg):
    log_entries.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "msg": msg})
    if len(log_entries) > 200: log_entries.pop()

# Template loaded from templates/dashboard.html via Flask render_template


# --- API ---

@app.route("/api/positions")
def api_positions():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        positions = mt5.get_positions()
        mt5.disconnect()
        result = []
        for p in positions:
            ticket = p["ticket"]
            be_info = _main.be_tracked.get(ticket, {})
            open_time = p.get("time", None)
            duration = ""
            if open_time:
                if isinstance(open_time, (int, float)):
                    open_time_dt = datetime.fromtimestamp(open_time)
                    open_time_str = str(open_time_dt)[:19]
                else:
                    open_time_str = str(open_time)[:19]
                    open_time_dt = open_time
                delta = datetime.now() - open_time_dt if isinstance(open_time_dt, datetime) else None
                if delta:
                    days = delta.days
                    hours, rem = divmod(delta.seconds, 3600)
                    mins = rem // 60
                    if days > 0: duration = f"{days}d {hours}h"
                    elif hours > 0: duration = f"{hours}h {mins}m"
                    else: duration = f"{mins}m"
            else:
                open_time_str = None
            entry_p = p["price_open"]
            sl_p = p.get("sl", 0)
            tp_p = p.get("tp", 0)
            sl_dist = tp_dist = 0
            if entry_p and sl_p: sl_dist = round((sl_p - entry_p) / entry_p * 100, 2)
            if entry_p and tp_p: tp_dist = round((tp_p - entry_p) / entry_p * 100, 2)
            result.append({
                "ticket": ticket, "symbol": p["symbol"], "type": p["type"],
                "volume": p["volume"], "entry": entry_p, "sl": sl_p, "tp": tp_p,
                "profit": p.get("profit", 0),
                "be_triggered": be_info.get("be_triggered", False),
                "open_time": open_time_str, "duration": duration,
                "sl_dist_pct": sl_dist, "tp_dist_pct": tp_dist,
            })
        return jsonify({"positions": result})
    except Exception as e:
        return jsonify({"positions": [], "error": str(e)})


@app.route("/api/stats")
def api_stats():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))

        mt5.connect()
        acc = mt5.get_account_summary()
        positions = mt5.get_positions()
        mt5.disconnect()

        open_pnl = sum(p.get("profit", 0) for p in positions)
        be_count = sum(1 for p in positions
                       if _main.be_tracked.get(p["ticket"], {}).get("be_triggered"))
        orders = database.get_order_history(5000)
        closed_orders = [o for o in orders if o.get("result") in ("win", "loss", "be")]
        total_trades = len(closed_orders)
        wins = sum(1 for o in closed_orders if o.get("result") == "win")
        losses = sum(1 for o in closed_orders if o.get("result") == "loss")
        closed_pnl = sum(o.get("pnl", 0) for o in closed_orders)
        wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        today = datetime.now().strftime("%Y-%m-%d")
        daily_pnl = sum(o.get("pnl", 0) for o in closed_orders if (o.get("time") or "").startswith(today))

        # Best/worst trade
        pnls = [o.get("pnl", 0) for o in closed_orders]
        best = max(pnls) if pnls else 0
        worst = min(pnls) if pnls else 0

        # Day-of-week
        dow_names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
        dow_pnl = {d: 0.0 for d in dow_names}
        for o in closed_orders:
            t = o.get("time", "")
            if t:
                try:
                    dt = datetime.fromisoformat(t[:19])
                    dow_pnl[dow_names[dt.weekday()]] += o.get("pnl", 0)
                except: pass

        # Monthly P&L
        monthly_pnl = {}
        for o in closed_orders:
            t = o.get("time", "")
            if t:
                monthly_pnl[t[:7]] = monthly_pnl.get(t[:7], 0) + o.get("pnl", 0)

        bal = acc.get("balance", 0)
        open_pnl_pct = round(open_pnl / bal * 100, 2) if bal > 0 else 0
        daily_pnl_pct = round(daily_pnl / bal * 100, 2) if bal > 0 else 0

        trading_days = database.count_trading_days()

        return jsonify({
            "balance": bal, "equity": acc.get("equity", 0),
            "positions_count": len(positions), "be_count": be_count,
            "open_pnl": open_pnl, "open_pnl_pct": open_pnl_pct,
            "total_trades": total_trades, "closed_pnl": closed_pnl,
            "win_rate": wr, "daily_pnl": daily_pnl, "daily_pnl_pct": daily_pnl_pct,
            "best_trade": best, "worst_trade": worst,
            "dow_pnl": dow_pnl, "monthly_pnl": monthly_pnl,
            "trading_days": trading_days,
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/prices")
def api_prices():
    try:
        from mt5_client import client as mt5
        import config
        mt5.connect()
        prices = {}
        for pair_name, symbol in config.PAIRS.items():
            tick = mt5.get_current_price(symbol)
            prices[pair_name] = {"bid": tick["bid"], "ask": tick["ask"]}
        mt5.disconnect()
        return jsonify(prices)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/equity")
def api_equity():
    """Return equity curve reconstructed from trade history.
    Initial balance = current MT5 balance - total closed P&L (no config needed)."""
    try:
        orders = database.get_order_history(5000)
        closed = [o for o in orders if o.get("result") in ("win", "loss", "be") and o.get("pnl")]
        closed.sort(key=lambda o: o.get("time", ""))
        total_closed_pnl = sum(o.get("pnl", 0) for o in closed)

        # Initial balance = current MT5 balance - total closed P&L
        from mt5_client import client as mt5
        mt5.connect()
        acc = mt5.get_account_summary()
        current_balance = float(acc.get("balance", 0))
        positions = mt5.get_positions()
        open_pnl = sum(p.get("profit", 0) for p in positions)
        mt5.disconnect()

        initial = current_balance - total_closed_pnl
        if initial <= 0:
            initial = current_balance or 10000

        pts = []
        cum = initial
        peak = initial
        max_dd_pct = 0.0

        if closed:
            pts.append({"t": closed[0].get("time", "")[:10], "e": round(initial, 2)})
        else:
            pts.append({"t": datetime.now().strftime("%Y-%m-%d"), "e": round(initial, 2)})

        for o in closed:
            pnl = o.get("pnl", 0)
            cum += pnl
            t = (o.get("time", "") or "")[:10]
            pts.append({"t": t, "e": round(cum, 2)})
            if cum > peak:
                peak = cum
            dd = (peak - cum) / peak * 100 if peak > 0 else 0
            if dd > max_dd_pct:
                max_dd_pct = dd

        current_equity = current_balance + open_pnl
        today_str = datetime.now().strftime("%Y-%m-%d")
        pts.append({"t": today_str, "e": round(current_equity, 2)})

        total_return = current_equity - initial
        total_return_pct = round(total_return / initial * 100, 2) if initial > 0 else 0

        return jsonify({
            "points": pts,
            "initial_balance": round(initial, 2),
            "current_equity": round(current_equity, 2),
            "total_return": round(total_return, 2),
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": round(max_dd_pct, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/log")
def api_log():
    n = request.args.get("n", 20, type=int)
    return jsonify({"log": log_entries[:n]})


@app.route("/api/export/csv")
def export_csv():
    orders = database.get_order_history(5000)
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["time","pair","direction","entry_price","sl_price","tp_price","volume","pnl","result"])
    for o in orders:
        w.writerow([o.get("time",""), o.get("pair",""), o.get("direction",""),
                    o.get("entry_price",""), o.get("sl_price",""), o.get("tp_price",""),
                    o.get("volume",""), o.get("pnl",""), o.get("result","")])
    output.seek(0)
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment;filename=trades.csv"})


# --- Pages ---

@app.route("/bot")
def dashboard():
    return render_template('dashboard.html', state=bot_state, tab="dashboard",
                                  positions=[], history=[], total_pnl=0, count=0,
                                  scan_interval=180, pair_filter="", date_filter="",
                                  auto_mode=bot_state.get("auto_mode", True),
                                  strategy_mode=bot_state.get("strategy_mode", "adx_tp"),
                                  risk_profile=bot_state.get("risk_profile", "challenge"))


@app.route("/bot/positions")
def positions_page():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        summary = mt5.get_positions_summary()
        positions = summary["positions"]
        for p in positions:
            p["be_triggered"] = _main.be_tracked.get(p["ticket"], {}).get("be_triggered", False)
        mt5.disconnect()
        total_pnl = summary["total_pnl"]; count = summary["count"]
    except Exception:
        positions = []; total_pnl = 0; count = 0
    return render_template('dashboard.html', positions=positions, total_pnl=total_pnl,
                                  count=count, tab="positions", state=bot_state,
                                  history=[], pair_filter="", date_filter="",
                                  strategy_mode=bot_state.get("strategy_mode", "adx_tp"),
                                  risk_profile=bot_state.get("risk_profile", "challenge"))


@app.route("/api/stats/detailed")
def api_stats_detailed():
    import config
    orders = database.get_order_history(5000)
    pairs = list(config.PAIRS.keys())  # Only active pairs

    def calc_pair_stats(pair_name):
        trades = [o for o in orders if o.get("pair") == pair_name]
        closed = [o for o in trades if o.get("result") in ("win", "loss")]
        wins = [o for o in closed if o.get("result") == "win"]
        losses = [o for o in closed if o.get("result") == "loss"]
        bes = [o for o in trades if o.get("result") == "be"]

        n = len(closed)
        w = len(wins)
        l = len(losses)
        wr = w / n * 100 if n > 0 else 0
        total_pnl = sum(o.get("pnl", 0) for o in closed)
        avg_win = sum(o.get("pnl", 0) for o in wins) / w if w > 0 else 0
        avg_loss = sum(o.get("pnl", 0) for o in losses) / l if l > 0 else 0
        gross_profit = sum(o.get("pnl", 0) for o in wins)
        gross_loss = abs(sum(o.get("pnl", 0) for o in losses))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Avg realized RR
        rr_values = []
        for o in closed:
            sl_d = abs(o.get("entry_price", 0) - o.get("sl_price", 0))
            tp_d = abs(o.get("tp_price", 0) - o.get("entry_price", 0))
            if sl_d > 0 and o.get("result") == "win":
                rr_values.append(tp_d / sl_d)

        avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0

        # Max DD (from balance history in trades)
        peak = 0; max_dd = 0
        for o in trades:
            bal = o.get("balance", 0)
            if bal > peak: peak = bal
            dd = (peak - bal) / peak * 100 if peak > 0 else 0
            if dd > max_dd: max_dd = dd

        # Sharpe (simplified: mean / std of P&L)
        pnls = [o.get("pnl", 0) for o in closed]
        mean_pnl = sum(pnls) / len(pnls) if pnls else 0
        std_pnl = (sum((x - mean_pnl)**2 for x in pnls) / len(pnls))**0.5 if pnls else 0
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0

        # Consecutive losses
        max_consec = 0; cur = 0
        for o in closed:
            if o.get("result") == "loss":
                cur += 1; max_consec = max(max_consec, cur)
            else:
                cur = 0

        return {
            "trades": n, "be": len(bes), "wins": w, "losses": l,
            "win_rate": round(wr, 1), "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
            "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
            "max_dd_pct": round(max_dd, 2),
            "sharpe": round(sharpe, 2),
            "avg_rr": round(avg_rr, 2),
            "max_consec_loss": max_consec,
            "best": round(max(o.get("pnl", 0) for o in closed), 2) if closed else 0,
            "worst": round(min(o.get("pnl", 0) for o in closed), 2) if closed else 0,
        }

    result = {}
    for p in pairs:
        result[p] = calc_pair_stats(p)
    return jsonify(result)


@app.route("/bot/stats")
def stats_page():
    return render_template('dashboard.html', tab="stats", state=bot_state,
                                  positions=[], history=[], total_pnl=0, count=0,
                                  scan_interval=180, pair_filter="", date_filter="",
                                  strategy_mode=bot_state.get("strategy_mode", "adx_tp"),
                                  risk_profile=bot_state.get("risk_profile", "challenge"))


@app.route("/bot/history")
def history_page():
    pair_filter = request.args.get("pair", "")
    date_filter = request.args.get("date", "")
    hist = database.get_order_history(500)
    # Only show trades with a result (closed)
    hist = [h for h in hist if h.get("result") in ("win", "loss", "be")]
    if pair_filter:
        hist = [h for h in hist if h.get("pair", "") == pair_filter]
    if date_filter:
        hist = [h for h in hist if (h.get("time", "") or "").startswith(date_filter)]
    hist = hist[:100]
    return render_template('dashboard.html', history=hist, tab="history", state=bot_state,
                                  pair_filter=pair_filter, date_filter=date_filter,
                                  strategy_mode=bot_state.get("strategy_mode", "adx_tp"),
                                  risk_profile=bot_state.get("risk_profile", "challenge"))


@app.route("/bot/close/<int:ticket>", methods=["POST"])
def close_one(ticket):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        info = _main.be_tracked.get(ticket, {})
        mt5.connect()
        # Capture live P&L BEFORE closing
        live_pnl = 0
        for p in mt5.get_positions():
            if p["ticket"] == ticket:
                live_pnl = p.get("profit", 0)
                break
        ok = mt5.close_position(ticket)
        if ok:
            # Log closed trade immediately
            try:
                entry_price = info.get("entry_price", 0)
                symbol = info.get("symbol", "")
                # Use live P&L if available, fall back to history
                pnl = live_pnl if live_pnl != 0 else info.get("last_profit", 0)
                if pnl == 0:
                    pnl, exit_price, volume = mt5.get_closed_trade_pnl(
                        ticket, hours=72, symbol=symbol, entry_price=entry_price)
                else:
                    exit_price, volume = 0, 0
                if abs(pnl) > 0.01:
                    result = "win" if pnl > 0 else "loss"
                else:
                    result = "be"
                if info:
                    direction = info.get("direction", "?")
                    pair = _main.symbol_to_pair(symbol) if hasattr(_main, 'symbol_to_pair') else symbol
                    database.save_closed_trade(
                        ticket=ticket, pair=pair, direction=direction,
                        entry_price=entry_price, sl_price=info.get("sl", 0),
                        tp_price=info.get("tp", 0), volume=0,
                        pnl=pnl, result=result, exit_price=0,
                        open_time=str(datetime.now()))
                    _main.be_tracked.pop(ticket, None)
                    database.clear_be_ticket(ticket)
                    database.save_be_state(_main.be_tracked)
                add_log(f"#{ticket} {result.upper()} ${pnl:+.2f}")
            except Exception as e:
                add_log(f"Закрыта позиция #{ticket} (лог: {e})")
        mt5.disconnect()
        if ok:
            if is_ajax: return jsonify({"ok": True, "msg": f"#{ticket} закрыта"})
        else:
            if is_ajax: return jsonify({"ok": False, "msg": f"Не удалось закрыть #{ticket}"})
    except Exception as e:
        if is_ajax: return jsonify({"ok": False, "msg": str(e)})
    return "<script>window.location='/bot/positions'</script>"


@app.route("/bot/be/<int:ticket>", methods=["POST"])
def move_to_be(ticket):
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        pos = mt5.get_positions()
        target = None
        for p in pos:
            if p["ticket"] == ticket:
                target = p
                break
        if target:
            entry = target["price_open"]
            current = target.get("price_current", 0)
            ptype = target["type"]  # 0=BUY, 1=SELL
            # Check: can only move SL to BE when position is in profit
            in_profit = (ptype == 0 and current > entry) or (ptype == 1 and current < entry)
            if not in_profit:
                mt5.disconnect()
                if is_ajax: return jsonify({"ok": False, "msg": "BE недоступен: позиция не в плюсе"})
                return "<script>alert('BE недоступен: позиция не в плюсе');window.location='/bot/positions'</script>"
            ok = mt5.modify_sl(ticket, entry)
            if ok:
                _main.be_tracked[ticket] = {"be_triggered": True, "entry_price": entry,
                                              "symbol": target["symbol"],
                                              "direction": "BUY" if target["type"] == 0 else "SELL"}
                add_log(f"#{ticket} SL → BE ({entry})")
                if is_ajax: return jsonify({"ok": True, "msg": f"#{ticket} → BE"})
            else:
                if is_ajax: return jsonify({"ok": False, "msg": f"Не удалось изменить SL #{ticket}"})
        mt5.disconnect()
    except Exception as e:
        if is_ajax: return jsonify({"ok": False, "msg": str(e)})
    return "<script>window.location='/bot/positions'</script>"


@app.route("/bot/close_all", methods=["POST"])
def close_all():
    try:
        from mt5_client import client as mt5
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        mt5.connect()
        # Capture be_tracked before closing (so we can log each)
        tracked_before = dict(_main.be_tracked)
        n = mt5.close_all_positions()
        # Log each closed position
        for ticket, info in tracked_before.items():
            try:
                symbol = info.get("symbol", "")
                entry_price = info.get("entry_price", 0)
                pnl, exit_price, volume = mt5.get_closed_trade_pnl(
                    ticket, hours=72, symbol=symbol, entry_price=entry_price)
                if abs(pnl) > 0.01:
                    result = "win" if pnl > 0 else "loss"
                else:
                    result = "be"
                pair = _main.symbol_to_pair(symbol) if hasattr(_main, 'symbol_to_pair') else symbol
                database.save_closed_trade(
                    ticket=ticket, pair=pair, direction=info.get("direction", "?"),
                    entry_price=info.get("entry_price", 0),
                    sl_price=info.get("sl", 0), tp_price=info.get("tp", 0),
                    volume=0, pnl=pnl, result=result, exit_price=0,
                    open_time=str(datetime.now()))
                _main.be_tracked.pop(ticket, None)
                database.clear_be_ticket(ticket)
            except Exception as e:
                print(f"[LOG] Error logging #{ticket}: {e}")
        database.save_be_state(_main.be_tracked)
        mt5.disconnect()
        add_log(f"Закрыто позиций: {n}")
    except Exception as e:
        add_log(f"Ошибка: {e}")
    return "<script>window.location='/bot/positions'</script>"


@app.route("/bot/scan", methods=["POST"])
def trigger_scan():
    """Trigger a manual scan now."""
    try:
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        _main.scan_all(is_manual=True)
        return {"ok": True, "msg": "Скан выполнен — проверьте лог"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

@app.route("/bot/mode", methods=["POST"])
def toggle_mode():
    """Toggle between auto and manual scan mode."""
    bot_state["auto_mode"] = not bot_state.get("auto_mode", True)
    mode = "авто" if bot_state["auto_mode"] else "ручной"
    add_log(f"Режим переключён: <b>{mode}</b>")
    return {"ok": True, "auto_mode": bot_state["auto_mode"], "msg": f"Режим: {mode}"}


@app.route("/bot/strategy", methods=["POST"])
def set_strategy():
    """Change trading strategy: fixed_rr | adx_tp | adx_h1."""
    mode = (request.get_json(silent=True) or {}).get("strategy", "adx_tp")
    if mode not in ("adx_tp", "adx_h1"):
        return {"ok": False, "msg": f"Unknown strategy: {mode}"}
    old = bot_state.get("strategy_mode", "adx_tp")
    bot_state["strategy_mode"] = mode
    labels = {"adx_tp": "ADX TP", "adx_h1": "ADX + H1"}
    add_log(f"Strategy: {labels.get(old, old)} -> <b>{labels.get(mode, mode)}</b>")
    if old == "adx_h1" and mode != "adx_h1":
        try:
            import main
            main.cancel_pending_signals()
        except Exception as e:
            print(f"[WEB] cancel_pending error: {e}")
    return {"ok": True, "strategy_mode": mode, "msg": f"Strategy: {labels[mode]}"}


@app.route("/bot/risk_profile", methods=["POST"])
def set_risk_profile():
    """Change risk profile: challenge | funded | custom."""
    profile = (request.get_json(silent=True) or {}).get("profile", "challenge")
    if profile not in ("challenge", "funded", "custom"):
        return {"ok": False, "msg": f"Unknown profile: {profile}"}
    old = bot_state.get("risk_profile", "challenge")
    bot_state["risk_profile"] = profile
    labels = {"challenge": "Challenge", "funded": "Funded", "custom": "Custom"}
    add_log(f"Risk: {labels.get(old, old)} -> <b>{labels.get(profile, profile)}</b>")
    return {"ok": True, "risk_profile": profile, "msg": f"Risk: {labels[profile]}"}


@app.route("/bot/resume", methods=["POST"])
def resume_bot():
    """Resume bot after total DD pause."""
    try:
        import risk_protection, main
        risk_protection.resume()
        if main.sched:
            main.sched.resume()
        add_log("<b>Бот возобновлён</b> — лимиты сброшены")
        return {"ok": True, "msg": "Bot resumed, limits reset"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.route("/api/healthcheck")
def api_healthcheck():
    """External monitoring endpoint. Returns 200 if bot is alive."""
    try:
        import main, risk_protection
        alive_sec = (datetime.now() - main._last_healthcheck).total_seconds()
        return {
            "ok": True,
            "last_healthcheck_sec": int(alive_sec),
            "bot_paused": risk_protection.bot_paused,
            "daily_stopped": risk_protection.daily_stopped,
        }
    except Exception:
        return {"ok": False}


@app.route("/bot/accept/<sig_id>", methods=["POST"])
def accept(sig_id):
    return "<script>alert('Авто-режим');window.location='/bot'</script>"

@app.route("/bot/reject/<sig_id>", methods=["POST"])
def reject(sig_id):
    return "<script>window.location='/bot'</script>"


def run_web(port=5002):
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

def start_web():
    t = threading.Thread(target=run_web, daemon=True)
    t.start()
    print(f"[Web] Dashboard v3 at http://localhost:5002/bot")
    return t
