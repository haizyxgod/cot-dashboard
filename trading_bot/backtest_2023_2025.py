"""
Backtest 2023-2025: $100,000 balance, FULL detailed statistics.
Compares adx_tp vs adx_h1 with granular breakdowns.
"""

import sys
import os
import pandas as pd
import numpy as np
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from mt5_client import MT5Client
from fvg_detector import check_fvg_signals
from fractal_detector import find_fractals, nearest_fractal, calculate_atr
from signal_engine import evaluate_setup
from risk_manager import calculate_lot
from trend_filter import check_daily_trend
from entry_trigger import check_h1_trigger
import config

mt5 = MT5Client()

# ---------------------------------------------------------------------------
# COT
# ---------------------------------------------------------------------------

def load_cot_signals():
    try:
        with open("cot_history.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("[WARN] cot_history.json not found")
        return None
    signals = {}
    for pair_key in ["XAU/USD", "USD/JPY"]:
        if pair_key not in raw: continue
        records = raw[pair_key]
        pair_signals = []
        for i, rec in enumerate(records):
            net = rec.get("leveraged_net") or rec.get("speculative_net", 0)
            prev_net = 0
            if i > 0:
                prev = records[i - 1]
                prev_net = prev.get("leveraged_net") or prev.get("speculative_net", 0)
            net_change = net - prev_net
            if net > 0 and net_change > 0: sig, score = "bullish", 80
            elif net > 0 and net_change <= 0: sig, score = "bullish", 50
            elif net < 0 and net_change < 0: sig, score = "bearish", 80
            elif net < 0 and net_change >= 0: sig, score = "bearish", 60
            else: sig, score = "neutral", 0
            direction = "bullish" if "bull" in sig else "bearish" if "bear" in sig else "neutral"
            if "JPY" in pair_key:
                inv = {"bullish": "bearish", "bearish": "bullish",
                       "strong_bullish": "strong_bearish", "strong_bearish": "strong_bullish"}
                sig = inv.get(sig, sig)
                direction = inv.get(direction, direction)
            pair_signals.append({"date": rec["date"], "signal": sig, "score": score,
                                 "direction": direction, "text": f"COT {sig}"})
        signals[pair_key] = pair_signals
    return signals

def get_cot_at_date(cot_signals, pair_name, target_date):
    if cot_signals is None or pair_name not in cot_signals:
        return {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}
    signals = cot_signals[pair_name]
    best = None
    for s in signals:
        if s["date"] <= target_date: best = s
        else: break
    return best or {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}

# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------

def _calc_adx(df, period=14):
    if len(df) < period + 1: return None
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    tr = np.zeros(len(high)); plus_dm = np.zeros(len(high)); minus_dm = np.zeros(len(high))
    for i in range(1, len(high)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        up, down = high[i] - high[i-1], low[i-1] - low[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
    atr_arr = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().values
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr_arr
        minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr_arr
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 0.0001)
    adx = pd.Series(dx).ewm(alpha=1/period, adjust=False).mean().values
    return float(adx[-1])

def _find_recent_fractal(df_vis, direction):
    if len(df_vis) < 6: return None
    df_f = find_fractals(df_vis)
    recent = df_f.tail(5)
    if direction == "BUY":
        ups = recent[recent["up_fractal"].notna()]
        return float(ups["up_fractal"].iloc[-1]) if not ups.empty else None
    else:
        downs = recent[recent["down_fractal"].notna()]
        return float(downs["down_fractal"].iloc[-1]) if not downs.empty else None

def _make_trade(ap, exit_time, pair_name, result, exit_price, pnl, balance, bar_idx,
                entry_bar=None):
    bars_held = bar_idx - (entry_bar if entry_bar is not None else ap["entry_bar"])
    return {
        "pair": pair_name, "direction": ap["direction"],
        "entry": round(ap["entry"], 5), "sl": round(ap.get("sl", 0), 5),
        "tp": round(ap.get("tp", 0), 5),
        "risk_amt": round(ap.get("risk_amt", 0), 2),
        "risk_pct": ap.get("risk_pct", 0), "volume": ap.get("volume", 0),
        "result": result, "exit_price": round(exit_price, 5),
        "exit_time": str(exit_time), "pnl": round(pnl, 2),
        "balance": round(balance, 2),
        "reason": ap.get("reason", ""), "score": ap.get("score", 0),
        "regime": ap.get("regime", "neutral"), "trigger": ap.get("trigger", ""),
        "_bar_idx": bar_idx, "bars_held": bars_held,
    }

def monte_carlo(trade_pnls, start_balance, n_sims=10000):
    rng = np.random.RandomState(42)
    finals = np.array([start_balance + rng.choice(trade_pnls, size=len(trade_pnls), replace=True).sum()
                       for _ in range(n_sims)])
    finals.sort()
    var_95 = start_balance - np.percentile(finals, 5)
    tail = finals[finals <= np.percentile(finals, 5)]
    cvar_95 = start_balance - tail.mean() if len(tail) > 0 else var_95
    ruin_prob = (finals < start_balance * 0.5).mean() * 100
    return {"var_95": var_95, "cvar_95": cvar_95, "ruin_prob": ruin_prob,
            "median_final": np.median(finals),
            "p10_final": np.percentile(finals, 10),
            "p90_final": np.percentile(finals, 90),
            "p25_final": np.percentile(finals, 25),
            "p75_final": np.percentile(finals, 75),
            "p5_final": np.percentile(finals, 5),
            "p95_final": np.percentile(finals, 95),
            "min_final": finals.min(), "max_final": finals.max()}


# ======================================================================
# BACKTEST ENGINE
# ======================================================================

def backtest_pair(symbol, pair_name, strategy_mode="adx_tp", start_balance=100000,
                   cot_signals=None, start_date="2023-01-01", end_date="2025-12-31",
                   verbose=True):
    is_forex = pair_name in ("GBP/USD", "USD/JPY")
    rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR
    use_h1 = (strategy_mode == "adx_h1")

    if verbose:
        label = "ADX TP + H1 trigger" if use_h1 else "ADX TP (H4 immediate)"
        print(f"\n  [{strategy_mode}] {pair_name}: {label}")

    cache_dir = os.path.join(os.path.dirname(__file__), "_backtest_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_suffix = "_H1" if use_h1 else ""
    cache_file = f"{cache_dir}/{symbol}_D_H4{cache_suffix}.pkl"

    if os.path.exists(cache_file):
        cached = pd.read_pickle(cache_file)
        all_d1, all_h4, all_h1 = cached["d1"], cached["h4"], cached.get("h1")
        pt, cs, tv = cached["point"], cached["contract_size"], cached["tick_value"]
    else:
        mt5.connect()
        all_d1 = mt5.get_candles(symbol, "D", 5000)
        all_h4 = mt5.get_candles(symbol, "H4", 40000)
        all_h1 = mt5.get_candles(symbol, "H1", 60000) if use_h1 else None
        info = mt5.get_symbol_info(symbol)
        mt5.disconnect()
        if all_d1.empty or all_h4.empty: return [], {}, start_balance
        pt = info["point"] if info else 0.01
        cs = info["trade_contract_size"] if info else 100
        tv = info["trade_tick_value"] if info else 0
        all_d1 = all_d1.sort_values("time").reset_index(drop=True)
        all_h4 = all_h4.sort_values("time").reset_index(drop=True)
        if all_h1 is not None: all_h1 = all_h1.sort_values("time").reset_index(drop=True)
        pd.to_pickle({"d1": all_d1, "h4": all_h4, "h1": all_h1,
                       "point": pt, "contract_size": cs, "tick_value": tv}, cache_file)

    if start_date:
        all_d1 = all_d1[all_d1["time"] >= pd.Timestamp(start_date)]
        all_h4 = all_h4[all_h4["time"] >= pd.Timestamp(start_date)]
        if all_h1 is not None: all_h1 = all_h1[all_h1["time"] >= pd.Timestamp(start_date)]
    if end_date:
        all_d1 = all_d1[all_d1["time"] <= pd.Timestamp(end_date)]
        all_h4 = all_h4[all_h4["time"] <= pd.Timestamp(end_date)]
        if all_h1 is not None: all_h1 = all_h1[all_h1["time"] <= pd.Timestamp(end_date)]
    all_d1 = all_d1.reset_index(drop=True)
    all_h4 = all_h4.reset_index(drop=True)
    if all_h1 is not None: all_h1 = all_h1.reset_index(drop=True)

    balance = start_balance
    trades = []; active_positions = []; pending_signals = []
    skip_cot = skip_fvg = skip_trend = skip_fractal = skip_cooldown = skip_no_trade = 0
    skip_be_wait = skip_trigger = 0
    _balance_history = [(all_h4.iloc[100]["time"], start_balance)]

    for i in range(100, len(all_h4)):
        current_time = all_h4.iloc[i]["time"]

        # --- 1. Process active ---
        still_open = []
        for ap in active_positions:
            if ap["entry_bar"] >= i:
                still_open.append(ap)
                continue
            if not ap["be_triggered"]:
                vis_be = all_h4.iloc[:i + 1]
                frac = _find_recent_fractal(vis_be, ap["direction"])
                if frac is not None:
                    if ap["direction"] == "BUY" and frac > ap["entry"]:
                        ap["be_triggered"] = True; ap["current_sl"] = ap["entry"]
                    elif ap["direction"] == "SELL" and frac < ap["entry"]:
                        ap["be_triggered"] = True; ap["current_sl"] = ap["entry"]

            sl_check = ap["entry"] if ap["be_triggered"] else ap["sl"]
            bar = all_h4.iloc[i]; closed = False
            if ap["direction"] == "BUY":
                if bar["low"] <= sl_check:
                    result = "be" if ap["be_triggered"] else "loss"
                    pnl = 0.0 if result == "be" else -ap["risk_amt"]
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, result,
                                               sl_check if result == "be" else ap["sl"],
                                               pnl, balance, i))
                    closed = True
                elif bar["high"] >= ap["tp"]:
                    r_mult = abs(ap["tp"] - ap["entry"]) / abs(ap["entry"] - ap["sl"])
                    pnl = ap["risk_amt"] * r_mult; balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win", ap["tp"], pnl, balance, i))
                    closed = True
            else:
                if bar["high"] >= sl_check:
                    result = "be" if ap["be_triggered"] else "loss"
                    pnl = 0.0 if result == "be" else -ap["risk_amt"]
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, result,
                                               sl_check if result == "be" else ap["sl"],
                                               pnl, balance, i))
                    closed = True
                elif bar["low"] <= ap["tp"]:
                    r_mult = abs(ap["tp"] - ap["entry"]) / abs(ap["entry"] - ap["sl"])
                    pnl = ap["risk_amt"] * r_mult; balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win", ap["tp"], pnl, balance, i))
                    closed = True
            if closed: _balance_history.append((current_time, balance))
            else: still_open.append(ap)
        active_positions = still_open

        # --- 2. Pending H1 triggers ---
        if use_h1 and all_h1 is not None:
            still_pending = []
            for ps in pending_signals:
                if i - ps["h4_signal_bar"] >= 6:
                    skip_trigger += 1; continue
                signal_h4_time = all_h4.iloc[ps["h4_signal_bar"]]["time"]
                h1_bars = all_h1[(all_h1["time"] > signal_h4_time) & (all_h1["time"] <= current_time)]
                triggered = False; entry_h1_price = None; trigger_type = ""
                for _, h1_bar in h1_bars.iterrows():
                    h1_ctx = all_h1.iloc[:h1_bar.name + 1].tail(30)
                    h4_ctx = all_h4.iloc[:i + 1].tail(5)
                    trig = check_h1_trigger(h1_ctx, h4_ctx, ps["direction"])
                    if trig["triggered"]:
                        entry_h1_price = h1_bar["close"]; triggered = True
                        trigger_type = trig.get("trigger_type", "h1"); break
                if triggered:
                    ap = {"entry_bar": i, "entry": entry_h1_price,
                          "sl": ps["sl"], "tp": ps["tp"], "direction": ps["direction"],
                          "risk_amt": ps["risk_amt"], "be_triggered": False,
                          "current_sl": ps["sl"], "risk_pct": ps["risk_pct"],
                          "volume": ps["volume"], "reason": ps["reason"] + " | H1 trig",
                          "score": ps["score"], "regime": ps["regime"], "trigger": trigger_type}
                    active_positions.append(ap)
                    _balance_history.append((current_time, balance))
                else:
                    still_pending.append(ps)
            pending_signals = still_pending

        # --- 3. Entry gates ---
        d1_trend = all_d1[all_d1["time"] <= current_time].tail(250)
        d1_fvg_data = all_d1[all_d1["time"] <= current_time].tail(20)
        h4_vis = all_h4.iloc[:i + 1].tail(50)
        if len(d1_fvg_data) < 10 or len(h4_vis) < 30: continue

        if active_positions:
            if len(active_positions) >= getattr(config, 'MAX_POSITIONS_PER_PAIR', 2):
                skip_be_wait += 1; continue
            if not all(p["be_triggered"] for p in active_positions):
                skip_be_wait += 1; continue
            if (i - max(p["entry_bar"] for p in active_positions)) < config.MIN_BARS_BETWEEN_TRADES:
                skip_cooldown += 1; continue
        else:
            if trades:
                last_close = max(t["_bar_idx"] for t in trades if t["pair"] == pair_name)
                if (i - last_close) < config.MIN_BARS_BETWEEN_TRADES:
                    skip_cooldown += 1; continue

        # --- 4. Signal ---
        fvg = check_fvg_signals(d1_fvg_data, h4_vis)
        if not fvg["direction"]: skip_fvg += 1; continue
        target_date = str(current_time)[:10]
        cot = get_cot_at_date(cot_signals, pair_name, target_date)
        trend = check_daily_trend(d1_trend, pair_name)
        signal = evaluate_setup(fvg, cot, trend)
        if not signal["trade"]:
            state = signal.get("state", "")
            if state == "cot_opposed": skip_cot += 1
            elif state == "trend_opposed": skip_trend += 1
            else: skip_no_trade += 1
            continue

        entry_price = all_h4.iloc[i]["close"]
        atr_val = calculate_atr(h4_vis, 14)
        sl = nearest_fractal(h4_vis, signal["fvg_direction"], entry_price, atr_value=atr_val)
        if sl is None: skip_fractal += 1; continue

        pos = calculate_lot(balance, entry_price, sl, signal["risk_pct"],
                            pair_name, pt, cs, tv, rr=rr)
        if pos.get("error"): continue

        # --- 5. ADX -> TP ---
        adx = _calc_adx(d1_trend, 14)
        if is_forex: trend_th, range_th = 20, 15
        else: trend_th, range_th = 25, 20

        if adx is None: regime = "neutral"
        elif adx > trend_th: regime = "trend"
        elif adx < range_th: regime = "range"
        else: regime = "neutral"

        if regime == "trend":
            tp_mult = config.TP_ATR_MULT_FOREX if is_forex else config.TP_ATR_MULT
            if atr_val and atr_val > 0:
                tp_price = entry_price + atr_val * tp_mult if signal["direction"] == "BUY" else entry_price - atr_val * tp_mult
            else: tp_price = pos["tp_price"]
        elif regime == "range":
            sl_dist = abs(entry_price - sl)
            tp_price = entry_price + sl_dist * 1.0 if signal["direction"] == "BUY" else entry_price - sl_dist * 1.0
        else: tp_price = pos["tp_price"]

        # --- 6. Entry ---
        if strategy_mode == "adx_h1" and all_h1 is not None:
            risk_amt = balance * (signal["risk_pct"] / 100)
            sl_dist = abs(entry_price - sl)
            vol = risk_amt / (sl_dist / pt * tv) if tv and tv > 0 else risk_amt / (sl_dist * cs)
            vol = round(max(0.01, vol), 2)
            pending_signals.append({"h4_signal_bar": i, "direction": signal["direction"],
                                    "sl": sl, "tp": tp_price, "risk_amt": risk_amt,
                                    "risk_pct": signal["risk_pct"], "volume": vol,
                                    "reason": signal["reason"], "score": signal.get("score", 0),
                                    "regime": regime})
        else:
            active_positions.append({"entry_bar": i, "entry": entry_price, "sl": sl,
                                     "tp": tp_price, "direction": signal["direction"],
                                     "risk_amt": pos["risk_amount"], "be_triggered": False,
                                     "current_sl": sl, "risk_pct": signal["risk_pct"],
                                     "volume": pos["volume"], "reason": signal["reason"],
                                     "score": signal.get("score", 0), "regime": regime,
                                     "trigger": "h4_immediate"})

    # Close remaining
    for ap in active_positions:
        result = "be" if ap["be_triggered"] else "open"
        trades.append(_make_trade(ap, all_h4.iloc[-1]["time"], pair_name, result,
                                   ap["entry"], 0.0, balance, ap["entry_bar"]))
    skip_trigger += len(pending_signals)

    if not trades:
        return [], {}, start_balance, pd.DataFrame(), [], skip_cot, skip_fvg, skip_trend, skip_fractal, skip_cooldown, skip_be_wait, skip_trigger

    df = pd.DataFrame(trades)
    closed = df[df["result"].isin(("win", "loss", "be"))]
    wins = closed[closed["result"] == "win"]; losses = closed[closed["result"] == "loss"]
    bes = closed[closed["result"] == "be"]

    total_trades = len(df); wins_n = len(wins); losses_n = len(losses); bes_n = len(bes)
    wr = wins_n / (wins_n + losses_n) * 100 if (wins_n + losses_n) > 0 else 0
    total_pnl = df["pnl"].sum(); final_balance = balance

    peak = start_balance; max_dd_pct = 0.0; dd_start = None; dd_end = None; peak_time = None
    current_dd_start = None
    for _, row in df.iterrows():
        bal = row["balance"]
        if bal > peak:
            peak = bal
            current_dd_start = None
        else:
            if current_dd_start is None: current_dd_start = row["exit_time"]
            dd_pct = (peak - bal) / peak * 100
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct; dd_start = current_dd_start; dd_end = row["exit_time"]

    avg_win = wins["pnl"].mean() if wins_n > 0 else 0
    avg_loss = abs(losses["pnl"].mean()) if losses_n > 0 else 0
    pf = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses_n > 0 and losses["pnl"].sum() != 0 else float("inf")

    pnl_series = df["pnl"].values
    sharpe = (pnl_series.mean() / pnl_series.std()) * np.sqrt(252) if pnl_series.std() > 0 else 0
    neg = pnl_series[pnl_series < 0]
    sortino = (pnl_series.mean() / neg.std()) * np.sqrt(252) if len(neg) > 0 and neg.std() > 0 else 0
    calmar = (total_pnl / start_balance * 100) / max_dd_pct if max_dd_pct > 0 else 0

    max_consec = 0; consec = 0; consec_wins = 0; max_consec_wins = 0
    for _, row in closed.iterrows():
        if row["result"] == "loss":
            consec += 1; consec_wins = 0
            if consec > max_consec: max_consec = consec
        elif row["result"] == "win":
            consec = 0; consec_wins += 1
            if consec_wins > max_consec_wins: max_consec_wins = consec_wins
        else:
            consec = 0; consec_wins = 0

    mc = monte_carlo(pnl_series, start_balance)

    # Monthly P&L
    df["exit_time_dt"] = pd.to_datetime(df["exit_time"])
    df["month"] = df["exit_time_dt"].dt.to_period("M")
    monthly = df.groupby("month")["pnl"].sum().sort_index()

    # R-multiple
    df["r_multiple"] = np.where(
        df["result"] == "win",
        df["pnl"] / df["risk_amt"],
        np.where(df["result"] == "loss", -1, 0)
    )

    # BARs held stats
    bars_held_arr = df[df["bars_held"].notna()]["bars_held"].values

    # Win/Loss streaks tracking
    streaks = []
    current_streak = 1
    for i in range(1, len(closed)):
        if closed.iloc[i]["result"] == closed.iloc[i-1]["result"]:
            current_streak += 1
        else:
            if current_streak > 1:
                streaks.append((closed.iloc[i-1]["result"], current_streak))
            current_streak = 1
    if current_streak > 1: streaks.append((closed.iloc[-1]["result"], current_streak))

    # Regime breakdown
    regime_stats = {}
    for reg in ["trend", "range", "neutral"]:
        reg_df = df[df["regime"] == reg]
        if len(reg_df) == 0: continue
        reg_closed = reg_df[reg_df["result"].isin(("win", "loss"))]
        reg_wins = reg_closed[reg_closed["result"] == "win"]
        reg_losses = reg_closed[reg_closed["result"] == "loss"]
        regime_stats[reg] = {
            "trades": len(reg_df), "wins": len(reg_wins), "losses": len(reg_losses),
            "be": len(reg_df[reg_df["result"] == "be"]),
            "wr": len(reg_wins) / (len(reg_wins) + len(reg_losses)) * 100 if (len(reg_wins) + len(reg_losses)) > 0 else 0,
            "pnl": reg_df["pnl"].sum(),
            "avg_win": reg_wins["pnl"].mean() if len(reg_wins) > 0 else 0,
            "avg_loss": abs(reg_losses["pnl"].mean()) if len(reg_losses) > 0 else 0,
        }

    stats = {
        "strategy": strategy_mode, "pair": pair_name, "symbol": symbol,
        "start_balance": start_balance, "final_balance": round(final_balance, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((final_balance - start_balance) / start_balance * 100, 2),
        "total_trades": total_trades, "wins": wins_n, "losses": losses_n, "be": bes_n,
        "win_rate": round(wr, 1), "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else float("inf"),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "dd_start": dd_start, "dd_end": dd_end,
        "best_trade": round(df["pnl"].max(), 2),
        "worst_trade": round(df["pnl"].min(), 2),
        "sharpe": round(sharpe, 2), "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_consec_losses": max_consec, "max_consec_wins": max_consec_wins,
        "var_95": round(mc["var_95"], 2), "cvar_95": round(mc["cvar_95"], 2),
        "ruin_prob": round(mc["ruin_prob"], 2),
        "mc_median": round(mc["median_final"], 2),
        "mc_p10": round(mc["p10_final"], 2),
        "mc_p90": round(mc["p90_final"], 2),
        "mc_p25": round(mc["p25_final"], 2),
        "mc_p75": round(mc["p75_final"], 2),
        "mc_p5": round(mc["p5_final"], 2),
        "mc_p95": round(mc["p95_final"], 2),
        "mc_min": round(mc["min_final"], 2),
        "mc_max": round(mc["max_final"], 2),
        "regime_stats": regime_stats,
        "monthly_pnl": monthly,
        "streaks": streaks,
        "avg_bars_held": float(np.mean(bars_held_arr)) if len(bars_held_arr) > 0 else 0,
        "median_bars_held": float(np.median(bars_held_arr)) if len(bars_held_arr) > 0 else 0,
        "avg_r_win": float(df[df["result"] == "win"]["r_multiple"].mean()) if wins_n > 0 else 0,
        "avg_r_loss": -1.0,
        "skipped": {"cot": skip_cot, "fvg": skip_fvg, "trend": skip_trend,
                     "fractal": skip_fractal, "cooldown": skip_cooldown,
                     "be_wait": skip_be_wait, "trigger": skip_trigger},
    }
    return trades, stats, final_balance, df, _balance_history, skip_cot, skip_fvg, skip_trend, skip_fractal, skip_cooldown, skip_be_wait, skip_trigger


# ======================================================================
# PRINTERS
# ======================================================================

def print_r_distribution(df, label):
    """Print R-multiple distribution table."""
    closed = df[df["result"].isin(("win", "loss"))]
    if closed.empty: return
    r_vals = closed["r_multiple"].values
    bins = [(-10, -3), (-3, -2), (-2, -1), (-1, 0), (0, 0.5), (0.5, 1), (1, 1.5),
            (1.5, 2), (2, 2.5), (2.5, 3), (3, 5), (5, 10), (10, 100)]
    print(f"\n  [{label}] R-Multiple Distribution:")
    print(f"  {'R Range':<15s} {'Count':>6s} {'%':>6s} {'Cum%':>7s} {'Histogram'}")
    print(f"  {'-'*55}")
    total = len(r_vals); cum = 0
    for lo, hi in bins:
        cnt = ((r_vals > lo) & (r_vals <= hi)).sum()
        if cnt == 0: continue
        pct = cnt / total * 100; cum += pct
        bar = "#" * max(1, int(pct * 2))
        print(f"  {lo:>4.0f} .. {hi:>4.0f}  {cnt:>6d}  {pct:>5.1f}%  {cum:>6.1f}%  {bar}")


def print_monthly_table(monthly_data, pairs_stats):
    """Print monthly P&L per strategy, per pair."""
    for strategy_mode in ["adx_tp", "adx_h1"]:
        print(f"\n  {'='*80}")
        print(f"  MONTHLY P&L: {strategy_mode}")
        print(f"  {'='*80}")

        for pair_name in ["XAU/USD", "USD/JPY"]:
            pair_s = next((s for s in pairs_stats[strategy_mode] if s and s["pair"] == pair_name), None)
            if pair_s is None: continue
            mp = pair_s["monthly_pnl"]
            yearly = defaultdict(float)
            print(f"\n  --- {pair_name} ---")
            print(f"  {'Month':<10s} {'P&L':>12s}  {'Cumulative':>12s}  {'Year Total':>12s}")
            print(f"  {'-'*50}")
            cum = 0
            for m, val in mp.items():
                yr = str(m).split("-")[0]; yearly[yr] += val; cum += val
                print(f"  {str(m):<10s} ${val:>+10,.0f}  ${cum:>+10,.0f}  ${yearly[yr]:>+10,.0f}")


def print_regime_analysis(all_stats):
    """Print per-regime performance for each strategy + pair."""
    for strategy_mode in ["adx_tp", "adx_h1"]:
        print(f"\n  {'='*80}")
        print(f"  REGIME PERFORMANCE: {strategy_mode}")
        print(f"  {'='*80}")
        for pair_name in ["XAU/USD", "USD/JPY"]:
            s = next((st for st in all_stats[strategy_mode] if st and st["pair"] == pair_name), None)
            if s is None: continue
            print(f"\n  --- {pair_name} ---")
            print(f"  {'Regime':<10s} {'Trades':>7s} {'Wins':>5s} {'Loss':>5s} {'BE':>5s} "
                  f"{'WR':>7s} {'P&L':>12s} {'Avg W':>10s} {'Avg L':>10s}")
            print(f"  {'-'*70}")
            for reg in ["trend", "range", "neutral"]:
                rs = s["regime_stats"].get(reg)
                if rs is None: continue
                print(f"  {reg:<10s} {rs['trades']:>7d} {rs['wins']:>5d} {rs['losses']:>5d} "
                      f"{rs['be']:>5d} {rs['wr']:>6.1f}% ${rs['pnl']:>+10,.0f} "
                      f"${rs['avg_win']:>8,.0f} ${rs['avg_loss']:>8,.0f}")


def print_equity_curve(balance_history, label):
    """Print equity curve summary with drawdown periods."""
    if not balance_history: return
    df_eq = pd.DataFrame(balance_history, columns=["time", "balance"])
    df_eq["time"] = pd.to_datetime(df_eq["time"])
    df_eq = df_eq.set_index("time").resample("W").last().ffill()

    peak = df_eq["balance"].iloc[0]
    dd_periods = []
    in_dd = False; dd_start = None; max_dd = 0; max_dd_start = None; max_dd_end = None

    for t, bal in df_eq["balance"].items():
        if bal >= peak:
            if in_dd and dd_start is not None:
                dd_periods.append((dd_start, t, round((peak - min_bal) / peak * 100, 1)))
            peak = bal; in_dd = False; dd_start = None
        else:
            if not in_dd: dd_start = t; min_bal = bal
            in_dd = True
            if bal < min_bal: min_bal = bal
            dd = (peak - bal) / peak * 100
            if dd > max_dd: max_dd = dd; max_dd_start = dd_start

    if in_dd and dd_start is not None:
        dd_periods.append((dd_start, list(df_eq.index)[-1],
                           round((peak - min_bal) / peak * 100, 1)))

    print(f"\n  [{label}] Equity Curve Summary:")
    print(f"  Start: ${balance_history[0][1]:,.0f}  |  End: ${balance_history[-1][1]:,.0f}")
    print(f"  Max Drawdown: {max_dd:.1f}% (from {str(max_dd_start)[:10]})")

    if dd_periods:
        significant_dds = [(s, e, d) for s, e, d in dd_periods if d > 3]
        if significant_dds:
            print(f"\n  Drawdown periods (>3%):")
            print(f"  {'From':<12s} {'To':<12s} {'DD %':>7s}")
            print(f"  {'-'*33}")
            for s, e, d in sorted(significant_dds, key=lambda x: x[2], reverse=True):
                print(f"  {str(s)[:10]:<12s} {str(e)[:10]:<12s} {d:>6.1f}%")


def print_trade_analysis(df, label):
    """Print detailed trade-by-trade analysis."""
    if df.empty: return
    closed = df[df["result"].isin(("win", "loss"))]
    wins = closed[closed["result"] == "win"]
    losses = closed[closed["result"] == "loss"]

    print(f"\n  [{label}] Trade Analysis:")

    # Win/Loss by score
    print(f"\n  By Signal Score:")
    print(f"  {'Score':<8s} {'Trades':>7s} {'Wins':>5s} {'Loss':>5s} {'BE':>5s} "
          f"{'WR':>7s} {'P&L':>12s}")
    print(f"  {'-'*52}")
    for sc in sorted(df["score"].unique()):
        sc_df = df[df["score"] == sc]
        sc_cl = sc_df[sc_df["result"].isin(("win", "loss"))]
        sc_w = sc_cl[sc_cl["result"] == "win"]
        sc_l = sc_cl[sc_cl["result"] == "loss"]
        wr_sc = len(sc_w) / (len(sc_w) + len(sc_l)) * 100 if (len(sc_w) + len(sc_l)) > 0 else 0
        print(f"  {int(sc):<8d} {len(sc_df):>7d} {len(sc_w):>5d} {len(sc_l):>5d} "
              f"{len(sc_df[sc_df['result']=='be']):>5d} {wr_sc:>6.1f}% ${sc_df['pnl'].sum():>+10,.0f}")

    # By direction
    print(f"\n  By Direction:")
    for d in ["BUY", "SELL"]:
        d_df = df[df["direction"] == d]
        d_cl = d_df[d_df["result"].isin(("win", "loss"))]
        d_w = d_cl[d_cl["result"] == "win"]; d_l = d_cl[d_cl["result"] == "loss"]
        wr_d = len(d_w) / (len(d_w) + len(d_l)) * 100 if (len(d_w) + len(d_l)) > 0 else 0
        print(f"  {d:<6s} Trades={len(d_df):>4d} W={len(d_w):>3d} L={len(d_l):>3d} "
              f"BE={len(d_df[d_df['result']=='be']):>3d} WR={wr_d:>5.1f}% P&L=${d_df['pnl'].sum():>+10,.0f}")

    # Hold time
    bars_arr = df[df["bars_held"].notna()]["bars_held"].values
    if len(bars_arr) > 0:
        print(f"\n  Hold Time (H4 bars):")
        print(f"  Avg: {np.mean(bars_arr):.1f} bars ({np.mean(bars_arr)*4:.0f}h)  |  "
              f"Median: {np.median(bars_arr):.1f} bars ({np.median(bars_arr)*4:.0f}h)  |  "
              f"Min: {bars_arr.min():.0f}  |  Max: {bars_arr.max():.0f}")

    # BE analysis
    bes = df[df["result"] == "be"]
    if len(bes) > 0 and len(bars_arr) > 0:
        be_bars = bes[bes["bars_held"].notna()]["bars_held"].values
        if len(be_bars) > 0:
            print(f"\n  BE Hold Time: Avg={np.mean(be_bars):.1f} bars ({np.mean(be_bars)*4:.0f}h) | "
                  f"Median={np.median(be_bars):.1f} bars")

    # Consecutive streaks
    streaks = []
    cur_s = 1
    for i in range(1, len(closed)):
        if closed.iloc[i]["result"] == closed.iloc[i-1]["result"]: cur_s += 1
        else:
            if cur_s > 1: streaks.append((closed.iloc[i-1]["result"], cur_s))
            cur_s = 1
    if cur_s > 1: streaks.append((closed.iloc[-1]["result"], cur_s))

    win_streaks = [n for r, n in streaks if r == "win"]
    loss_streaks = [n for r, n in streaks if r == "loss"]
    if win_streaks:
        print(f"\n  Win Streaks:  Max={max(win_streaks)}  Avg={np.mean(win_streaks):.1f}  "
              f"Count={len(win_streaks)}")
    if loss_streaks:
        print(f"  Loss Streaks: Max={max(loss_streaks)}  Avg={np.mean(loss_streaks):.1f}  "
              f"Count={len(loss_streaks)}")


# ======================================================================
# MAIN
# ======================================================================

def main():
    print("=" * 80)
    print("  DETAILED BACKTEST: adx_tp vs adx_h1")
    print("  2023-2025 | $100,000 | XAU/USD + USD/JPY")
    print("=" * 80)

    print("\nLoading COT history...")
    cot_signals = load_cot_signals()

    pairs = [("GOLD.pro", "XAU/USD"), ("USDJPY.pro", "USD/JPY")]

    all_stats = {"adx_tp": [], "adx_h1": []}
    all_dfs = {"adx_tp": [], "adx_h1": []}
    all_balance_histories = {"adx_tp": [], "adx_h1": []}
    all_skip_reasons = {"adx_tp": {}, "adx_h1": {}}

    for strategy_mode in ["adx_tp", "adx_h1"]:
        print(f"\n{'#'*70}\n#  {strategy_mode}\n{'#'*70}")
        for symbol, pair_name in pairs:
            trades, stats, final_bal, df, bal_hist, skip_cot, skip_fvg, skip_trend, \
                skip_fractal, skip_cooldown, skip_be_wait, skip_trigger = \
                backtest_pair(symbol, pair_name, strategy_mode=strategy_mode,
                              start_balance=100000, cot_signals=cot_signals,
                              start_date="2023-01-01", end_date="2025-12-31", verbose=True)
            if stats:
                all_stats[strategy_mode].append(stats)
                all_balance_histories[strategy_mode].append((pair_name, bal_hist))
            if trades:
                all_dfs[strategy_mode].append(df)

    # ==================================================================
    # 1. PER-PAIR DETAILED METRICS
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  1. PER-PAIR DETAILED METRICS")
    print("=" * 80)

    pair_metrics = {}
    for strategy_mode in ["adx_tp", "adx_h1"]:
        pair_metrics[strategy_mode] = {}
        for s in all_stats[strategy_mode]:
            pair_metrics[strategy_mode][s["pair"]] = s

    for pair_name in ["XAU/USD", "USD/JPY"]:
        print(f"\n  {'='*70}")
        print(f"  {pair_name}")
        print(f"  {'='*70}")

        header = f"  {'Metric':<30s}"
        for m in ["adx_tp", "adx_h1"]:
            header += f" {m:>18s}"
        print(header)
        print(f"  {'-'*68}")

        all_rows = [
            ("Total Trades", "total_trades", "d"),
            ("Wins", "wins", "d"),
            ("Losses", "losses", "d"),
            ("BE (Break Even)", "be", "d"),
            ("Win Rate (excl BE)", "win_rate", ".1f%%"),
            ("", None, ""),
            ("Total P&L", "total_pnl", "$,.0f"),
            ("Total Return", "total_return_pct", ".1f%%"),
            ("Final Balance", "final_balance", "$,.0f"),
            ("Profit Factor", "profit_factor", ".2f"),
            ("Max Drawdown", "max_drawdown_pct", ".1f%%"),
            ("", None, ""),
            ("Avg Win", "avg_win", "$,.0f"),
            ("Avg Loss", "avg_loss", "$,.0f"),
            ("Avg R-Multiple (Win)", "avg_r_win", ".2fR"),
            ("Best Trade", "best_trade", "$,.0f"),
            ("Worst Trade", "worst_trade", "$,.0f"),
            ("", None, ""),
            ("Avg Bars Held", "avg_bars_held", ".1f"),
            ("Median Bars Held", "median_bars_held", ".1f"),
            ("Max Consec Wins", "max_consec_wins", "d"),
            ("Max Consec Losses", "max_consec_losses", "d"),
            ("", None, ""),
            ("Sharpe Ratio", "sharpe", ".2f"),
            ("Sortino Ratio", "sortino", ".2f"),
            ("Calmar Ratio", "calmar", ".2f"),
            ("", None, ""),
            ("VaR 95% (MC 10k)", "var_95", "$,.0f"),
            ("CVaR 95% (MC 10k)", "cvar_95", "$,.0f"),
            ("Ruin Probability", "ruin_prob", ".1f%%"),
            ("MC Median Final", "mc_median", "$,.0f"),
            ("MC P10 Final", "mc_p10", "$,.0f"),
            ("MC P90 Final", "mc_p90", "$,.0f"),
            ("MC P25 Final", "mc_p25", "$,.0f"),
            ("MC P75 Final", "mc_p75", "$,.0f"),
            ("MC P5 Final", "mc_p5", "$,.0f"),
            ("MC P95 Final", "mc_p95", "$,.0f"),
            ("MC Min Final", "mc_min", "$,.0f"),
            ("MC Max Final", "mc_max", "$,.0f"),
        ]

        for name, key, fmt in all_rows:
            if key is None:
                print()
                continue
            line = f"  {name:<30s}"
            for m in ["adx_tp", "adx_h1"]:
                val = pair_metrics[m].get(pair_name, {})
                if not val:
                    line += f" {'N/A':>18s}"
                    continue
                v = val[key]
                if fmt == "d":
                    line += f" {int(v):>18d}"
                elif fmt in (".1f%%", ".2f%%"):
                    line += f" {v:>17.1f}%" if ".1f" in fmt else f" {v:>17.2f}%"
                elif fmt in (".1f", ".2f"):
                    line += f" {v:>18.1f}" if ".1f" in fmt else f" {v:>18.2f}"
                elif fmt == "$,.0f":
                    line += f" ${v:>17,.0f}"
                elif fmt == ".2fR":
                    line += f" {v:>17.2f}R"
            print(line)

        # Skip reasons
        print(f"\n  Skip Reasons:")
        skip_keys = [("COT blocked", "cot"), ("No FVG", "fvg"), ("Trend opposed", "trend"),
                     ("No Fractal SL", "fractal"), ("Cooldown", "cooldown"),
                     ("BE wait", "be_wait"), ("No H1 trigger", "trigger")]
        print(f"  {'Reason':<20s}", end="")
        for m in ["adx_tp", "adx_h1"]:
            print(f" {m:>18s}", end="")
        print()
        for label, key in skip_keys:
            line = f"  {label:<20s}"
            for m in ["adx_tp", "adx_h1"]:
                val = pair_metrics[m].get(pair_name, {})
                if val:
                    line += f" {int(val['skipped'].get(key, 0)):>18d}"
                else:
                    line += f" {'N/A':>18s}"
            print(line)

    # ==================================================================
    # 2. R-MULTIPLE DISTRIBUTION
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  2. R-MULTIPLE DISTRIBUTION")
    print("=" * 80)

    for strategy_mode in ["adx_tp", "adx_h1"]:
        for pair_name in ["XAU/USD", "USD/JPY"]:
            s = next((st for st in all_stats[strategy_mode] if st["pair"] == pair_name), None)
            if s is None: continue
            df = next((d for d in all_dfs[strategy_mode] if d["pair"].iloc[0] == pair_name), None)
            if df is not None:
                print_r_distribution(df, f"{strategy_mode} | {pair_name}")

    # ==================================================================
    # 3. MONTHLY P&L PER PAIR
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  3. MONTHLY P&L BY STRATEGY & PAIR")
    print("=" * 80)

    for strategy_mode in ["adx_tp", "adx_h1"]:
        print(f"\n  {'='*70}")
        print(f"  {strategy_mode}")
        print(f"  {'='*70}")
        for pair_name in ["XAU/USD", "USD/JPY"]:
            s = next((st for st in all_stats[strategy_mode] if st["pair"] == pair_name), None)
            if s is None: continue
            mp = s["monthly_pnl"]
            print(f"\n  --- {pair_name} ---")
            yearly = defaultdict(float); cum_total = 0
            print(f"  {'Month':<10s} {'P&L':>12s} {'Cumulative':>12s} {'Year':>5s} {'Yr Total':>12s}")
            print(f"  {'-'*58}")
            for m, val in mp.items():
                yr = str(m).split("-")[0]; yearly[yr] += val; cum_total += val
                print(f"  {str(m):<10s} ${val:>+10,.0f} ${cum_total:>+10,.0f} {yr:>5s} ${yearly[yr]:>+10,.0f}")

            # Quarterly summary
            print(f"\n  Quarterly Summary:")
            print(f"  {'Quarter':<10s} {'Q P&L':>12s}")
            print(f"  {'-'*24}")
            q_data = defaultdict(float)
            for m, val in mp.items():
                yr = str(m).split("-")[0]
                mo = int(str(m).split("-")[1])
                q = f"{yr}-Q{(mo-1)//3+1}"
                q_data[q] += val
            for q in sorted(q_data.keys()):
                print(f"  {q:<10s} ${q_data[q]:>+10,.0f}")

    # ==================================================================
    # 4. REGIME ANALYSIS
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  4. REGIME PERFORMANCE (Trend / Range / Neutral)")
    print("=" * 80)

    print_regime_analysis(all_stats)

    # ==================================================================
    # 5. EQUITY CURVE & DRAWDOWN ANALYSIS
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  5. EQUITY CURVE & DRAWDOWN PERIODS")
    print("=" * 80)

    for strategy_mode in ["adx_tp", "adx_h1"]:
        for pair_name, bal_hist in all_balance_histories[strategy_mode]:
            print_equity_curve(bal_hist, f"{strategy_mode} | {pair_name}")

    # ==================================================================
    # 6. TRADE ANALYSIS (score, direction, hold time, streaks)
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  6. DETAILED TRADE ANALYSIS")
    print("=" * 80)

    for strategy_mode in ["adx_tp", "adx_h1"]:
        for pair_name in ["XAU/USD", "USD/JPY"]:
            df = next((d for d in all_dfs[strategy_mode] if d["pair"].iloc[0] == pair_name), None)
            if df is not None:
                print_trade_analysis(df, f"{strategy_mode} | {pair_name}")

    # ==================================================================
    # 7. COMBINED SUMMARY
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  7. COMBINED SUMMARY (XAU/USD + USD/JPY)")
    print("=" * 80)

    for strategy_mode in ["adx_tp", "adx_h1"]:
        dfs = all_dfs[strategy_mode]
        if not dfs: continue
        df_all = pd.concat(dfs, ignore_index=True)
        total_pnl = df_all["pnl"].sum()
        total_t = len(df_all)
        closed = df_all[df_all["result"].isin(("win", "loss"))]
        w = closed[closed["result"] == "win"]; l = closed[closed["result"] == "loss"]
        wr = len(w) / (len(w) + len(l)) * 100 if (len(w) + len(l)) > 0 else 0

        print(f"\n  [{strategy_mode}]")
        print(f"  Total Trades: {total_t}  |  W={len(w)}  L={len(l)}  "
              f"BE={len(df_all[df_all['result']=='be'])}  |  WR={wr:.1f}%")
        print(f"  Total P&L: ${total_pnl:+,.0f}  |  Avg/Trade: ${total_pnl/total_t:+,.0f}")

        # Monthly combined
        df_all["exit_time_dt"] = pd.to_datetime(df_all["exit_time"])
        df_all["month"] = df_all["exit_time_dt"].dt.to_period("M")
        monthly_all = df_all.groupby("month")["pnl"].sum().sort_index()

        print(f"\n  Monthly Summary:")
        print(f"  {'Month':<10s} {'P&L':>12s} {'Cumulative':>12s}")
        print(f"  {'-'*36}")
        cum = 0
        for m, val in monthly_all.items():
            cum += val
            print(f"  {str(m):<10s} ${val:>+10,.0f} ${cum:>+10,.0f}")

        # Annual
        print(f"\n  Annual:")
        yearly_all = defaultdict(float)
        for m, val in monthly_all.items():
            yearly_all[str(m).split("-")[0]] += val
        for yr in sorted(yearly_all):
            print(f"  {yr}: ${yearly_all[yr]:>+10,.0f}")

    # ==================================================================
    # 8. FINAL VERDICT
    # ==================================================================
    print("\n\n" + "=" * 80)
    print("  8. FINAL VERDICT")
    print("=" * 80)

    for strategy_mode in ["adx_tp", "adx_h1"]:
        dfs = all_dfs[strategy_mode]
        if not dfs: continue
        df_all = pd.concat(dfs, ignore_index=True)
        total_pnl = df_all["pnl"].sum()

        print(f"\n  [{strategy_mode}]")
        for pair_name in ["XAU/USD", "USD/JPY"]:
            s = next((st for st in all_stats[strategy_mode] if st["pair"] == pair_name), None)
            if s:
                print(f"    {pair_name:>10s}: ${s['total_pnl']:>+10,.0f}  "
                      f"({s['total_return_pct']:>+6.1f}%)  WR={s['win_rate']:.1f}%  "
                      f"DD={s['max_drawdown_pct']:.1f}%  PF={s['profit_factor']}")
        print(f"    {'TOTAL':>10s}: ${total_pnl:>+10,.0f}")

    # Delta
    tp_pnl = sum(s["total_pnl"] for s in all_stats["adx_tp"])
    h1_pnl = sum(s["total_pnl"] for s in all_stats["adx_h1"])
    tp_trades = sum(s["total_trades"] for s in all_stats["adx_tp"])
    h1_trades = sum(s["total_trades"] for s in all_stats["adx_h1"])

    print(f"\n  H1 TRIGGER VALUE ADDED over ADX TP:")
    print(f"    Additional P&L:    ${h1_pnl - tp_pnl:+,.0f}")
    print(f"    P&L Multiplier:    {h1_pnl/tp_pnl:.1f}x")
    print(f"    Trade Count Delta: {h1_trades - tp_trades:+d}")
    print(f"    Avg Trade P&L:     ${tp_pnl/tp_trades:+,.0f} -> ${h1_pnl/h1_trades:+,.0f}")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    main()
