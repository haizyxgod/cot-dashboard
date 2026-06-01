"""Quick backtest: night-only entries (hour < 11), 2023-2026."""
import pandas as pd
import numpy as np
from datetime import datetime
import sys, os, json

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cot_dashboard"))

from mt5_client import client as mt5
from fvg_detector import check_fvg_signals
from fractal_detector import nearest_fractal, calculate_atr, find_fractals
from signal_engine import evaluate_setup
from risk_manager import calculate_lot
from trend_filter import check_daily_trend
import config

# ---------------------------------------------------------------------------
# COT loader (same as backtest.py)
# ---------------------------------------------------------------------------
def load_cot_history():
    path = os.path.join(os.path.dirname(__file__), "cot_history.json")
    if not os.path.exists(path):
        print("[WARN] cot_history.json not found")
        return None
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cot_signals = {}
    for pair_name, records in raw.items():
        if pair_name == "metadata":
            continue
        signals = []
        for i, rec in enumerate(records):
            net = rec.get("speculative_net") or rec.get("leveraged_net", 0)
            prev_net = records[i-1].get("speculative_net") or records[i-1].get("leveraged_net", 0) if i > 0 else 0
            net_change = net - prev_net
            if net > 0 and net_change > 0:
                sig, score = "strong_bullish", 80
            elif net > 0 and net_change <= 0:
                sig, score = "bullish", 60
            elif net < 0 and net_change < 0:
                sig, score = "strong_bearish", 80
            elif net < 0 and net_change >= 0:
                sig, score = "bearish", 60
            else:
                sig, score = "neutral", 0
            direction = "bullish" if "bull" in sig else "bearish" if "bear" in sig else "neutral"
            if "JPY" in pair_name:
                if direction == "bullish": direction, sig = "bearish", "strong_bearish" if "strong" in sig else "bearish"
                elif direction == "bearish": direction, sig = "bullish", "strong_bullish" if "strong" in sig else "bullish"
            signals.append({"date": rec["date"], "signal": sig, "score": score, "direction": direction, "text": f"COT {sig} (net={net:+,})"})
        cot_signals[pair_name] = signals
    return cot_signals

def get_cot_at_date(cot_signals, pair_name, target_date):
    if cot_signals is None or pair_name not in cot_signals:
        return {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}
    best = None
    for s in cot_signals[pair_name]:
        if s["date"] <= target_date: best = s
        else: break
    return best or {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _find_recent_fractal(df_vis, direction):
    if len(df_vis) < 7: return None
    df_f = find_fractals(df_vis)
    recent = df_f.tail(5)
    if direction == "BUY":
        ups = recent[recent["up_fractal"].notna()]; return ups["up_fractal"].iloc[-1] if not ups.empty else None
    else:
        downs = recent[recent["down_fractal"].notna()]; return downs["down_fractal"].iloc[-1] if not downs.empty else None

def _calc_adx(df, period=14):
    if len(df) < period + 1: return None
    high, low, close = df["high"].values, df["low"].values, df["close"].values
    tr = np.zeros(len(high)); plus_dm = np.zeros(len(high)); minus_dm = np.zeros(len(high))
    for i in range(1, len(high)):
        tr[i] = max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        up, down = high[i]-high[i-1], low[i-1]-low[i]
        plus_dm[i] = up if up > down and up > 0 else 0
        minus_dm[i] = down if down > up and down > 0 else 0
    atr = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().values
    pdi = 100 * pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr
    mdi = 100 * pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 0.0001)
    return float(pd.Series(dx).ewm(alpha=1/period, adjust=False).mean().values[-1])

def _make_trade(ap, exit_time, pair_name, result, exit_price, pnl, balance, bar_idx):
    return {
        "time": str(exit_time)[:19], "pair": pair_name, "direction": ap["direction"],
        "entry": round(ap["entry"], 6), "sl": round(ap["sl"], 6), "tp": round(ap["tp"], 6),
        "risk_pct": ap.get("risk_pct", 0), "volume": ap.get("volume", 0),
        "risk_amt": ap.get("risk_amt", 0), "result": result,
        "exit_price": round(exit_price, 6) if exit_price else None,
        "exit_time": str(exit_time)[:19], "bars_held": bar_idx - ap["entry_bar"],
        "pnl": round(pnl, 2), "balance": round(balance, 2),
        "reason": ap.get("reason", ""), "score": ap.get("score", 0),
        "_bar_idx": bar_idx,
    }

# ---------------------------------------------------------------------------
# Backtest with session filter
# ---------------------------------------------------------------------------
def backtest_night(symbol, pair_name, start_balance=10000, cot_signals=None,
                   start_date=None, end_date=None, session_end_hour=11, verbose=True):
        is_forex = pair_name in ("GBP/USD", "USD/JPY")
    rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR

    mt5.connect()
    all_d1 = mt5.get_candles(symbol, "D", 5000)
    all_h4 = mt5.get_candles(symbol, "H4", 40000)
    info = mt5.get_symbol_info(symbol)
    mt5.disconnect()

    if all_d1.empty or all_h4.empty:
        print(f"  [SKIP] No data for {symbol}")
        return [], {}, start_balance

    pt = info["point"] if info else (0.00001 if is_forex else 0.01)
    cs = info["trade_contract_size"] if info else (100000 if is_forex else 100)
    tv = info["trade_tick_value"] if info else 0

    all_d1 = all_d1.sort_values("time").reset_index(drop=True)
    all_h4 = all_h4.sort_values("time").reset_index(drop=True)

    if start_date:
        all_d1 = all_d1[all_d1["time"] >= pd.Timestamp(start_date)].reset_index(drop=True)
        all_h4 = all_h4[all_h4["time"] >= pd.Timestamp(start_date)].reset_index(drop=True)
    if end_date:
        all_d1 = all_d1[all_d1["time"] <= pd.Timestamp(end_date)].reset_index(drop=True)
        all_h4 = all_h4[all_h4["time"] <= pd.Timestamp(end_date)].reset_index(drop=True)

    print(f"  {pair_name}: {len(all_h4)} H4 bars, {all_h4['time'].iloc[0]} -> {all_h4['time'].iloc[-1]}")

    balance = start_balance
    trades = []
    active_positions = []
    skip_session = 0; skip_fvg = 0; skip_cot = 0; skip_trend = 0
    skip_fractal = 0; skip_cooldown = 0; skip_no_trade = 0; skip_be_wait = 0

    for i in range(100, len(all_h4)):
        current_time = all_h4.iloc[i]["time"]

        # --- Process active positions ---
        still_open = []
        for ap in active_positions:
            if ap["entry_bar"] >= i:
                still_open.append(ap); continue
            if not ap["be_triggered"]:
                vis_be = all_h4.iloc[:i+1]
                frac = _find_recent_fractal(vis_be, ap["direction"])
                if frac is not None:
                    if ap["direction"] == "BUY" and frac > ap["entry"]:
                        ap["be_triggered"] = True; ap["current_sl"] = ap["entry"]
                    elif ap["direction"] == "SELL" and frac < ap["entry"]:
                        ap["be_triggered"] = True; ap["current_sl"] = ap["entry"]

            sl_check = ap["entry"] if ap["be_triggered"] else ap["sl"]
            bar = all_h4.iloc[i]
            closed = False
            if ap["direction"] == "BUY":
                if bar["low"] <= sl_check:
                    result = "be" if ap["be_triggered"] else "loss"
                    pnl = 0.0 if result == "be" else -ap["risk_amt"]
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, result, sl_check if result=="be" else ap["sl"], pnl, balance, i))
                    closed = True
                elif bar["high"] >= ap["tp"]:
                    r_mult = abs(ap["tp"]-ap["entry"])/abs(ap["entry"]-ap["sl"])
                    pnl = ap["risk_amt"]*r_mult; balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win", ap["tp"], pnl, balance, i))
                    closed = True
            else:
                if bar["high"] >= sl_check:
                    result = "be" if ap["be_triggered"] else "loss"
                    pnl = 0.0 if result == "be" else -ap["risk_amt"]
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, result, sl_check if result=="be" else ap["sl"], pnl, balance, i))
                    closed = True
                elif bar["low"] <= ap["tp"]:
                    r_mult = abs(ap["tp"]-ap["entry"])/abs(ap["entry"]-ap["sl"])
                    pnl = ap["risk_amt"]*r_mult; balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win", ap["tp"], pnl, balance, i))
                    closed = True
            if not closed:
                still_open.append(ap)
        active_positions = still_open

        # --- Entry gates ---
        d1_trend = all_d1[all_d1["time"] <= current_time].tail(250)
        d1_fvg_data = all_d1[all_d1["time"] <= current_time].tail(20)
        h4_vis = all_h4.iloc[:i+1].tail(50)
        if len(d1_fvg_data) < 10 or len(h4_vis) < 30: continue

        if active_positions:
            if len(active_positions) >= getattr(config, 'MAX_POSITIONS_PER_PAIR', 2):
                skip_be_wait += 1; continue
            all_be = all(p["be_triggered"] for p in active_positions)
            if not all_be: skip_be_wait += 1; continue
            last_entry = max(p["entry_bar"] for p in active_positions)
            if (i - last_entry) < config.MIN_BARS_BETWEEN_TRADES: skip_cooldown += 1; continue
        else:
            if trades:
                last_close = max(t["_bar_idx"] for t in trades if t["pair"] == pair_name)
                if (i - last_close) < config.MIN_BARS_BETWEEN_TRADES: skip_cooldown += 1; continue

        fvg = check_fvg_signals(d1_fvg_data, h4_vis)
        if not fvg["direction"]: skip_fvg += 1; continue

        target_date = str(current_time)[:10]
        cot = get_cot_at_date(cot_signals, pair_name, target_date)
        trend = check_daily_trend(d1_trend, pair_name)
        signal = evaluate_setup(fvg, cot, trend)
        if not signal["trade"]:
            state = signal.get("state","")
            if state=="cot_opposed": skip_cot += 1
            elif state=="trend_opposed": skip_trend += 1
            else: skip_no_trade += 1
            continue

        entry_price = all_h4.iloc[i]["close"]
        atr_val = calculate_atr(h4_vis, 14)
        sl = nearest_fractal(h4_vis, signal["fvg_direction"], entry_price, atr_value=atr_val)
        if sl is None: skip_fractal += 1; continue

        pos = calculate_lot(balance, entry_price, sl, signal["risk_pct"], pair_name, pt, cs, tv, rr=rr)
        if pos.get("error"): continue

        adx = _calc_adx(d1_trend, 14)
        trend_thresh, range_thresh = (20, 15) if is_forex else (25, 20)
        if adx is None: regime = "neutral"
        elif adx > trend_thresh: regime = "trend"
        elif adx < range_thresh: regime = "range"
        else: regime = "neutral"

        if regime == "trend":
            tp_mult = config.TP_ATR_MULT_FOREX if is_forex else config.TP_ATR_MULT
            tp_price = entry_price + atr_val*tp_mult if signal["direction"]=="BUY" else entry_price - atr_val*tp_mult if atr_val else pos["tp_price"]
        elif regime == "range":
            sl_dist = abs(entry_price-sl)
            tp_price = entry_price + sl_dist if signal["direction"]=="BUY" else entry_price - sl_dist
        else:
            tp_price = pos["tp_price"]

        active_positions.append({
            "entry_bar": i, "entry": entry_price, "sl": sl, "tp": tp_price,
            "direction": signal["direction"], "risk_amt": pos["risk_amount"],
            "be_triggered": False, "current_sl": sl,
            "risk_pct": signal["risk_pct"], "volume": pos["volume"],
            "reason": signal["reason"], "score": signal.get("score", 0), "regime": regime,
        })

    # Close remaining
    for ap in active_positions:
        trades.append(_make_trade(ap, all_h4.iloc[-1]["time"], pair_name,
                                   "be" if ap["be_triggered"] else "open",
                                   ap["entry"], 0.0, balance, ap["entry_bar"]))

    if not trades: return [], {}, start_balance

    df = pd.DataFrame(trades)
    closed = df[df["result"].isin(("win","loss","be"))]
    wins, losses, bes = closed[closed["result"]=="win"], closed[closed["result"]=="loss"], closed[closed["result"]=="be"]
    wins_n, losses_n, bes_n = len(wins), len(losses), len(bes)
    wr = wins_n/(wins_n+losses_n)*100 if (wins_n+losses_n)>0 else 0
    total_pnl = df["pnl"].sum()

    peak = start_balance; max_dd_pct = 0.0
    for b in df["balance"]:
        if b > peak: peak = b
        dd_pct = (peak-b)/peak*100
        if dd_pct > max_dd_pct: max_dd_pct = dd_pct

    avg_win = wins["pnl"].mean() if wins_n>0 else 0
    avg_loss = abs(losses["pnl"].mean()) if losses_n>0 else 0
    pf = (wins["pnl"].sum()/abs(losses["pnl"].sum())) if losses_n>0 and losses["pnl"].sum()!=0 else float("inf")

    stats = {
        "pair": pair_name, "total_trades": len(df), "wins": wins_n, "losses": losses_n,
        "be": bes_n, "open": len(df)-wins_n-losses_n-bes_n, "win_rate": round(wr,1),
        "total_pnl": round(total_pnl,2), "final_balance": round(balance,2),
        "total_return_pct": round((balance-start_balance)/start_balance*100,2),
        "avg_win": round(avg_win,2), "avg_loss": round(avg_loss,2),
        "profit_factor": round(pf,2) if pf!=float("inf") else "inf",
        "max_drawdown_pct": round(max_dd_pct,2),
        "best_trade": round(df["pnl"].max(),2), "worst_trade": round(df["pnl"].min(),2),
        "session_skipped": skip_session,
    }

    print(f"  Trades:{stats['total_trades']} W:{wins_n} L:{losses_n} BE:{bes_n} WR:{wr:.1f}% "
          f"P&L:${total_pnl:+,.2f} DD:{max_dd_pct:.1f}% PF:{pf:.2f} "
          f"Skipped(session):{skip_session}")
    return trades, stats, balance


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
PERIODS = [
    ("2010", "2010-01-01", "2010-12-31"),
    ("2011", "2011-01-01", "2011-12-31"),
    ("2012", "2012-01-01", "2012-12-31"),
    ("2013", "2013-01-01", "2013-12-31"),
    ("2014", "2014-01-01", "2014-12-31"),
    ("2015", "2015-01-01", "2015-12-31"),
    ("2016", "2016-01-01", "2016-12-31"),
    ("2017", "2017-01-01", "2017-12-31"),
    ("2018", "2018-01-01", "2018-12-31"),
    ("2019", "2019-01-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026", "2026-01-01", "2026-05-27"),
]

def main():
    print("=" * 60)
    print("  FULL BACKTEST (no session filter)")
    print("  Pairs: XAU/USD + USD/JPY  |  2010–2026")
    print("=" * 60)

    cot_signals = load_cot_history()
    all_trades = []; all_stats = []

    for period_name, start, end in PERIODS:
        print(f"\n--- {period_name} ---")
        for pair_name, symbol in config.PAIRS.items():
            trades, stats, bal = backtest_night(symbol, pair_name, start_balance=10000,
                                                 cot_signals=cot_signals,
                                                 start_date=start, end_date=end,
                                                 session_end_hour=11, verbose=True)
            all_trades.extend(trades); all_stats.append(stats)

    if not all_trades:
        print("\n[NO TRADES]"); return

    df_all = pd.DataFrame(all_trades)
    closed = df_all[df_all["result"].isin(("win","loss"))]
    wins, losses = closed[closed["result"]=="win"], closed[closed["result"]=="loss"]
    bes = df_all[df_all["result"]=="be"]
    total_trades = len(df_all); wins_n = len(wins); losses_n = len(losses)
    wr = wins_n/(wins_n+losses_n)*100 if (wins_n+losses_n)>0 else 0
    total_pnl = df_all["pnl"].sum()
    pf = (wins["pnl"].sum()/abs(losses["pnl"].sum())) if losses_n>0 and losses["pnl"].sum()!=0 else float("inf")

    print("\n" + "=" * 60)
    print(f"  FULL BACKTEST SUMMARY (2010-2026)")
    print(f"  {'Pair':<10s} {'Trades':>6s} {'Win':>4s} {'Loss':>5s} {'BE':>5s} {'WR':>6s} {'P&L':>10s} {'DD':>7s}")
    print("  " + "-" * 60)
    for s in all_stats:
        if s:
            print(f"  {s['pair']:<10s} {s['total_trades']:>6d} {s['wins']:>4d} {s['losses']:>5d} "
                  f"{s['be']:>5d} {s['win_rate']:>5.1f}% ${s['total_pnl']:>+9,.2f} {s['max_drawdown_pct']:>6.1f}%")
    print(f"  {'TOTAL':<10s} {total_trades:>6d} {wins_n:>4d} {losses_n:>5d} {len(bes):>5d} "
          f"{wr:>5.1f}% ${total_pnl:>+9,.2f}  PF={pf:.2f}")
    print("=" * 60)

    # Save trades to CSV
    if all_trades:
        df_all.to_csv(os.path.join(os.path.dirname(__file__), "backtest_fractal_be.csv"), index=False)
        print("\n  [Saved] backtest_fractal_be.csv")

if __name__ == "__main__":
    main()
