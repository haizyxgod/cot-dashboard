"""
Comparative backtest: Current Bot vs Adaptive TP + H1 Trigger
Period: 2016-2026 | Pairs: XAU/USD + USD/JPY

Variant A (current):  Fixed RR TP + H4 entry + Fractal BE  [prod mirror]
Variant B (adaptive): ADX TP + H1 trigger entry + Fractal BE
"""

import sys
import pandas as pd
import numpy as np
from collections import defaultdict

sys.path.insert(0, '.')
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
# COT helpers (simplified from backtest.py)
# ---------------------------------------------------------------------------

def load_cot_signals():
    """Pre-compute COT signals for every available date."""
    import json
    try:
        with open("cot_history.json", "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("  [WARN] cot_history.json not found — COT disabled")
        return None

    signals = {}
    for pair_key in ["XAU/USD", "USD/JPY"]:
        if pair_key not in raw:
            continue
        records = raw[pair_key]
        pair_signals = []
        for i, rec in enumerate(records):
            if "leveraged_net" in rec:
                net = rec["leveraged_net"]
            elif "speculative_net" in rec:
                net = rec["speculative_net"]
            else:
                net = 0

            if i > 0:
                prev = records[i - 1]
                if "leveraged_net" in prev:
                    prev_net = prev["leveraged_net"]
                elif "speculative_net" in prev:
                    prev_net = prev["speculative_net"]
                else:
                    prev_net = 0
                net_change = net - prev_net
            else:
                net_change = 0

            if net > 0 and net_change > 0:
                sig = "bullish"; score = 80
            elif net > 0 and net_change <= 0:
                sig = "bullish"; score = 50
            elif net < 0 and net_change < 0:
                sig = "bearish"; score = 80
            elif net < 0 and net_change >= 0:
                sig = "bearish"; score = 60
            else:
                sig = "neutral"; score = 0

            direction = "bullish" if "bull" in sig else "bearish" if "bear" in sig else "neutral"

            # JPY inversion
            if "JPY" in pair_key:
                inv_map = {"bullish": "bearish", "bearish": "bullish",
                           "strong_bullish": "strong_bearish",
                           "strong_bearish": "strong_bullish"}
                sig = inv_map.get(sig, sig)
                direction = inv_map.get(direction, direction)

            pair_signals.append({
                "date": rec["date"],
                "signal": sig,
                "score": score,
                "direction": direction,
                "text": f"COT {sig}",
            })
        signals[pair_key] = pair_signals
    return signals


def get_cot_at_date(cot_signals, pair_name, target_date):
    if cot_signals is None or pair_name not in cot_signals:
        return {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}
    signals = cot_signals[pair_name]
    best = None
    for s in signals:
        if s["date"] <= target_date:
            best = s
        else:
            break
    if best is None:
        return {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}
    return best


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Fractal BE helper
# ---------------------------------------------------------------------------

def _find_recent_fractal(df_vis, direction):
    """Find most recent fractal in trade direction from last 5 bars."""
    if len(df_vis) < 6:
        return None
    df_f = find_fractals(df_vis)
    recent = df_f.tail(5)
    if direction == "BUY":
        ups = recent[recent["up_fractal"].notna()]
        if not ups.empty:
            return float(ups["up_fractal"].iloc[-1])
    else:
        downs = recent[recent["down_fractal"].notna()]
        if not downs.empty:
            return float(downs["down_fractal"].iloc[-1])
    return None


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

def _make_trade(ap, exit_time, pair_name, result, exit_price, pnl, balance, bar_idx):
    return {
        "pair": pair_name,
        "direction": ap["direction"],
        "entry": round(ap["entry"], 5),
        "sl": round(ap.get("sl", 0), 5),
        "tp": round(ap.get("tp", 0), 5),
        "risk_amt": round(ap.get("risk_amt", 0), 2),
        "risk_pct": ap.get("risk_pct", 0),
        "volume": ap.get("volume", 0),
        "result": result,
        "exit_price": round(exit_price, 5),
        "exit_time": str(exit_time),
        "pnl": round(pnl, 2),
        "balance": round(balance, 2),
        "reason": ap.get("reason", ""),
        "score": ap.get("score", 0),
        "regime": ap.get("regime", "neutral"),
        "trigger": ap.get("trigger", ""),
        "_bar_idx": bar_idx,
    }


# ---------------------------------------------------------------------------
# Main backtest function
# ---------------------------------------------------------------------------

def backtest_variant(symbol, pair_name, variant, start_balance=10000,
                     cot_signals=None, start_date="2016-01-01", end_date="2026-01-01",
                     verbose=True):
    """
    variant: 'current' | 'v4_adaptive' | 'adaptive_h1'
    """
    is_forex = pair_name in ("GBP/USD", "USD/JPY")
    rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR

    label = {"current": "CURRENT (fixed RR, H4 entry)",
             "v4_adaptive": "v4 + ADAPTIVE TP (ADX TP, H4 entry)",
             "adaptive_h1": "ADAPTIVE (ADX TP + H1 trigger)"}[variant]

    need_h1 = (variant == "adaptive_h1")

    if verbose:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"  {pair_name} ({symbol}) | {start_date} -> {end_date}")
        print(f"{'='*60}")

    # --- Data loading with CSV cache ---
    import os
    cache_dir = "_backtest_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = f"{cache_dir}/{symbol}_D_H4{'_H1' if variant == 'adaptive_h1' else ''}.pkl"

    if os.path.exists(cache_file):
        if verbose:
            print(f"  [CACHE] Loading from {cache_file}")
        cached = pd.read_pickle(cache_file)
        all_d1 = cached["d1"]
        all_h4 = cached["h4"]
        all_h1 = cached.get("h1")
        pt = cached["point"]
        cs = cached["contract_size"]
        tv = cached["tick_value"]
    else:
        if verbose:
            print(f"  [MT5] Downloading candles...")
        mt5.connect()
        all_d1 = mt5.get_candles(symbol, "D", 5000)
        all_h4 = mt5.get_candles(symbol, "H4", 40000)
        all_h1 = mt5.get_candles(symbol, "H1", 60000) if need_h1 else None
        info = mt5.get_symbol_info(symbol)
        mt5.disconnect()

        if all_d1.empty or all_h4.empty:
            print(f"  [SKIP] No data for {symbol}")
            return [], {}, start_balance

        pt = info["point"] if info else (0.00001 if "GBP" in symbol else 0.01)
        cs = info["trade_contract_size"] if info else (100000 if "GBP" in symbol else 100)
        tv = info["trade_tick_value"] if info else 0

        all_d1 = all_d1.sort_values("time").reset_index(drop=True)
        all_h4 = all_h4.sort_values("time").reset_index(drop=True)
        if all_h1 is not None:
            all_h1 = all_h1.sort_values("time").reset_index(drop=True)

        # Save to cache
        pd.to_pickle({"d1": all_d1, "h4": all_h4, "h1": all_h1,
                       "point": pt, "contract_size": cs, "tick_value": tv},
                      cache_file)
        if verbose:
            print(f"  [CACHE] Saved to {cache_file}")

    # Filter date range
    if start_date:
        all_d1 = all_d1[all_d1["time"] >= pd.Timestamp(start_date)]
        all_h4 = all_h4[all_h4["time"] >= pd.Timestamp(start_date)]
        if all_h1 is not None:
            all_h1 = all_h1[all_h1["time"] >= pd.Timestamp(start_date)]
    if end_date:
        all_d1 = all_d1[all_d1["time"] <= pd.Timestamp(end_date)]
        all_h4 = all_h4[all_h4["time"] <= pd.Timestamp(end_date)]
        if all_h1 is not None:
            all_h1 = all_h1[all_h1["time"] <= pd.Timestamp(end_date)]

    all_d1 = all_d1.reset_index(drop=True)
    all_h4 = all_h4.reset_index(drop=True)
    if all_h1 is not None:
        all_h1 = all_h1.reset_index(drop=True)

    if verbose:
        print(f"  D1: {len(all_d1)} | H4: {len(all_h4)}"
              + (f" | H1: {len(all_h1)}" if all_h1 is not None else ""))

    balance = start_balance
    trades = []
    active_positions = []
    pending_signals = []  # only for H1 trigger variant

    skip_cot = skip_fvg = skip_trend = skip_fractal = skip_cooldown = skip_no_trade = 0
    skip_be_wait = skip_trigger = 0

    for i in range(100, len(all_h4)):
        current_time = all_h4.iloc[i]["time"]

        # ====================================================
        # 1. Process active positions (same for both variants)
        # ====================================================
        still_open = []
        for ap in active_positions:
            if ap["entry_bar"] >= i:
                still_open.append(ap)
                continue

            # Fractal BE
            if not ap["be_triggered"]:
                vis_be = all_h4.iloc[:i + 1]
                frac = _find_recent_fractal(vis_be, ap["direction"])
                if frac is not None:
                    if ap["direction"] == "BUY" and frac > ap["entry"]:
                        ap["be_triggered"] = True
                        ap["current_sl"] = ap["entry"]
                    elif ap["direction"] == "SELL" and frac < ap["entry"]:
                        ap["be_triggered"] = True
                        ap["current_sl"] = ap["entry"]

            sl_check = ap["entry"] if ap["be_triggered"] else ap["sl"]
            bar = all_h4.iloc[i]
            closed = False

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
                    pnl = ap["risk_amt"] * r_mult
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win",
                                               ap["tp"], pnl, balance, i))
                    closed = True
            else:  # SELL
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
                    pnl = ap["risk_amt"] * r_mult
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win",
                                               ap["tp"], pnl, balance, i))
                    closed = True

            if not closed:
                still_open.append(ap)
        active_positions = still_open

        # ====================================================
        # 2. Check pending H1 triggers (H1 variant only)
        # ====================================================
        if need_h1:
            still_pending = []
            for ps in pending_signals:
                h4_bars_waited = i - ps["h4_signal_bar"]
                if h4_bars_waited >= 6:  # 24h = 6 H4 bars
                    skip_trigger += 1
                    continue

                # Get H1 bars since the signal H4 bar
                signal_h4_time = all_h4.iloc[ps["h4_signal_bar"]]["time"]
                h1_bars = all_h1[(all_h1["time"] > signal_h4_time) &
                                 (all_h1["time"] <= current_time)]

                triggered = False
                entry_h1_price = None
                for _, h1_bar in h1_bars.iterrows():
                    h1_idx = h1_bar.name
                    # Context: H1 bars up to trigger point, H4 bars up to current
                    h1_ctx = all_h1.iloc[:h1_idx + 1].tail(30)
                    h4_ctx = all_h4.iloc[:i + 1].tail(5)
                    trig = check_h1_trigger(h1_ctx, h4_ctx, ps["direction"])
                    if trig["triggered"]:
                        entry_h1_price = h1_bar["close"]
                        triggered = True
                        break

                if triggered:
                    ap = {
                        "entry_bar": i,  # current H4 bar
                        "entry": entry_h1_price,
                        "sl": ps["sl"],
                        "tp": ps["tp"],
                        "direction": ps["direction"],
                        "risk_amt": ps["risk_amt"],
                        "be_triggered": False,
                        "current_sl": ps["sl"],
                        "risk_pct": ps["risk_pct"],
                        "volume": ps["volume"],
                        "reason": ps["reason"] + " | H1 trig",
                        "score": ps["score"],
                        "regime": ps["regime"],
                        "trigger": trig.get("trigger_type", "h1"),
                    }
                    active_positions.append(ap)
                else:
                    still_pending.append(ps)
            pending_signals = still_pending

        # ====================================================
        # 3. Entry gates (cooldown / pyramiding)
        # ====================================================
        d1_trend = all_d1[all_d1["time"] <= current_time].tail(250)
        d1_fvg_data = all_d1[all_d1["time"] <= current_time].tail(20)
        h4_vis = all_h4.iloc[:i + 1].tail(50)

        if len(d1_fvg_data) < 10 or len(h4_vis) < 30:
            continue

        # Pyramiding gate
        if active_positions:
            if len(active_positions) >= getattr(config, 'MAX_POSITIONS_PER_PAIR', 2):
                skip_be_wait += 1
                continue
            all_be = all(p["be_triggered"] for p in active_positions)
            if not all_be:
                skip_be_wait += 1
                continue
            last_entry = max(p["entry_bar"] for p in active_positions)
            if (i - last_entry) < config.MIN_BARS_BETWEEN_TRADES:
                skip_cooldown += 1
                continue
        else:
            if trades:
                last_close = max(t["_bar_idx"] for t in trades if t["pair"] == pair_name)
                if (i - last_close) < config.MIN_BARS_BETWEEN_TRADES:
                    skip_cooldown += 1
                    continue

        # ====================================================
        # 4. Signal evaluation (same for both variants)
        # ====================================================
        fvg = check_fvg_signals(d1_fvg_data, h4_vis)
        if not fvg["direction"]:
            skip_fvg += 1
            continue

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
        sl = nearest_fractal(h4_vis, signal["fvg_direction"], entry_price,
                             atr_value=atr_val)
        if sl is None:
            skip_fractal += 1
            continue

        pos = calculate_lot(balance, entry_price, sl, signal["risk_pct"],
                            pair_name, pt, cs, tv, rr=rr)
        if pos.get("error"):
            continue

        # ====================================================
        # 5. TP calculation (differs by variant)
        # ====================================================
        if variant == "current":
            # Fixed RR — use calculate_lot result directly
            tp_price = pos["tp_price"]
            regime = "fixed"

        else:
            # ADX regime detection (v4_adaptive + adaptive_h1)
            adx = _calc_adx(d1_trend, 14)
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
                        tp_price = entry_price + atr_val * tp_mult
                    else:
                        tp_price = entry_price - atr_val * tp_mult
                else:
                    tp_price = pos["tp_price"]
            elif regime == "range":
                sl_dist = abs(entry_price - sl)
                if signal["direction"] == "BUY":
                    tp_price = entry_price + sl_dist * 1.0
                else:
                    tp_price = entry_price - sl_dist * 1.0
            else:
                tp_price = pos["tp_price"]

        # ====================================================
        # 6. Entry (differs by variant)
        # ====================================================
        if variant in ("current", "v4_adaptive"):
            # Enter immediately at H4 close
            ap = {
                "entry_bar": i,
                "entry": entry_price,
                "sl": sl,
                "tp": tp_price,
                "direction": signal["direction"],
                "risk_amt": pos["risk_amount"],
                "be_triggered": False,
                "current_sl": sl,
                "risk_pct": signal["risk_pct"],
                "volume": pos["volume"],
                "reason": signal["reason"],
                "score": signal.get("score", 0),
                "regime": regime if variant == "v4_adaptive" else "fixed",
                "trigger": "",
            }
            active_positions.append(ap)

        elif variant == "adaptive_h1":
            # Queue as pending — wait for H1 trigger
            risk_amt = balance * (signal["risk_pct"] / 100)
            sl_dist = abs(entry_price - sl)
            vol = risk_amt / (sl_dist / pt * tv) if tv and tv > 0 else risk_amt / (sl_dist * cs)
            vol = round(vol, 2)
            vol = max(0.01, vol)

            pending_signals.append({
                "h4_signal_bar": i,
                "direction": signal["direction"],
                "sl": sl,
                "tp": tp_price,
                "risk_amt": risk_amt,
                "risk_pct": signal["risk_pct"],
                "volume": vol,
                "reason": signal["reason"],
                "score": signal.get("score", 0),
                "regime": regime,
            })

    # Close remaining positions
    for ap in active_positions:
        result = "be" if ap["be_triggered"] else "open"
        pnl = 0.0
        trades.append(_make_trade(ap, all_h4.iloc[-1]["time"], pair_name, result,
                                   ap["entry"], pnl, balance, ap["entry_bar"]))

    # Pending signals that never triggered — count as skipped
    skip_trigger += len(pending_signals)

    if not trades:
        if verbose:
            print(f"  [NO TRADES] COT={skip_cot} FVG={skip_fvg} Trend={skip_trend} "
                  f"NoTrade={skip_no_trade} Fractal={skip_fractal} Cooldown={skip_cooldown}")
        return [], {}, start_balance

    # ====================================================
    # Statistics
    # ====================================================
    df = pd.DataFrame(trades)
    closed = df[df["result"].isin(("win", "loss", "be"))]
    wins = closed[closed["result"] == "win"]
    losses = closed[closed["result"] == "loss"]
    bes = closed[closed["result"] == "be"]
    opens = df[df["result"] == "open"]

    total_trades = len(df)
    wins_n = len(wins)
    losses_n = len(losses)
    bes_n = len(bes)
    wr = wins_n / (wins_n + losses_n) * 100 if (wins_n + losses_n) > 0 else 0
    total_pnl = df["pnl"].sum()
    final_balance = balance

    peak = start_balance; max_dd_pct = 0.0
    for bal in df["balance"]:
        if bal > peak: peak = bal
        dd_pct = (peak - bal) / peak * 100
        if dd_pct > max_dd_pct: max_dd_pct = dd_pct

    avg_win = wins["pnl"].mean() if wins_n > 0 else 0
    avg_loss = abs(losses["pnl"].mean()) if losses_n > 0 else 0
    pf = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses_n > 0 and losses["pnl"].sum() != 0 else float("inf")

    # Sharpe / Sortino / Calmar
    pnl_series = df["pnl"].values
    sharpe = (pnl_series.mean() / pnl_series.std()) * np.sqrt(252) if pnl_series.std() > 0 else 0
    neg = pnl_series[pnl_series < 0]
    sortino = (pnl_series.mean() / neg.std()) * np.sqrt(252) if len(neg) > 0 and neg.std() > 0 else 0
    calmar = (total_pnl / start_balance * 100) / max_dd_pct if max_dd_pct > 0 else 0

    # Consecutive losses
    max_consec = 0; consec = 0
    for _, row in closed.iterrows():
        if row["result"] == "loss":
            consec += 1
            if consec > max_consec: max_consec = consec
        else:
            consec = 0

    stats = {
        "variant": variant,
        "pair": pair_name, "symbol": symbol,
        "start_balance": start_balance,
        "final_balance": round(final_balance, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((final_balance - start_balance) / start_balance * 100, 2),
        "total_trades": total_trades, "wins": wins_n, "losses": losses_n,
        "be": bes_n, "open": len(opens), "win_rate": round(wr, 1),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else float("inf"),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "best_trade": round(df["pnl"].max(), 2),
        "worst_trade": round(df["pnl"].min(), 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "max_consec_losses": max_consec,
        "skipped_cot": skip_cot, "skipped_fvg": skip_fvg,
        "skipped_trend": skip_trend, "skipped_fractal": skip_fractal,
        "skipped_cooldown": skip_cooldown, "skipped_be_wait": skip_be_wait,
        "skipped_trigger": skip_trigger,
    }

    if verbose:
        print(f"\n  {'-'*52}")
        print(f"  TRADES: {total_trades} | W={wins_n} L={losses_n} BE={bes_n} "
              f"| WR={wr:.1f}% | PF={pf:.2f}")
        print(f"  P&L: ${total_pnl:+,.0f} | Return: {stats['total_return_pct']:+.1f}% "
              f"| DD: {max_dd_pct:.1f}%")
        print(f"  Sharpe: {sharpe:.2f} | Sortino: {sortino:.2f} | Calmar: {calmar:.2f}")
        print(f"  Skipped: COT={skip_cot} FVG={skip_fvg} Trend={skip_trend} "
              f"Fractal={skip_fractal} Trigger={skip_trigger}")

    return trades, stats, final_balance


# ---------------------------------------------------------------------------
# Run comparison
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  COMPARATIVE BACKTEST")
    print("  Current Bot vs Adaptive TP + H1 Trigger")
    print("  2016-2026 | XAU/USD + USD/JPY")
    print("=" * 60)

    # Load COT history
    print("\nLoading COT history...")
    cot_signals = load_cot_signals()
    if cot_signals:
        print(f"  COT loaded: {list(cot_signals.keys())}")
    else:
        print("  COT not available — will use neutral for all signals")

    pairs = [
        ("GOLD.pro", "XAU/USD"),
        ("USDJPY.pro", "USD/JPY"),
    ]

    all_stats = []

    for symbol, pair_name in pairs:
        for variant in ["current", "v4_adaptive", "adaptive_h1"]:
            trades, stats, final_bal = backtest_variant(
                symbol, pair_name, variant,
                start_balance=10000, cot_signals=cot_signals,
                start_date="2016-01-01", end_date="2026-01-01",
                verbose=True,
            )
            all_stats.append(stats)

    # ====================================================
    # COMPARISON TABLE
    # ====================================================
    print("\n")
    print("=" * 90)
    print("  FINAL COMPARISON")
    print("=" * 90)
    print()

    variants = ["current", "v4_adaptive", "adaptive_h1"]
    vlabels = {"current": "CURRENT (fixed RR)", "v4_adaptive": "v4 + ADAPTIVE TP", "adaptive_h1": "v5 (ADX TP + H1)"}

    # Per-pair comparison
    for pair_name in ["XAU/USD", "USD/JPY"]:
        print(f"  {'-'*86}")
        print(f"  {pair_name}")
        print(f"  {'-'*86}")
        header = f"  {'Metric':<28s}"
        for v in variants:
            header += f" {vlabels[v]:>19s}"
        print(header)
        print(f"  {'-'*86}")

        st = {v: next(s for s in all_stats if s["pair"] == pair_name and s["variant"] == v) for v in variants}

        metrics = [
            ("Total Trades", "total_trades", "d"),
            ("Wins", "wins", "d"),
            ("Losses", "losses", "d"),
            ("BE", "be", "d"),
            ("Win Rate", "win_rate", ".1f"),
            ("Total P&L ($)", "total_pnl", ",.0f"),
            ("Return %", "total_return_pct", ".1f"),
            ("Profit Factor", "profit_factor", ".2f"),
            ("Max Drawdown %", "max_drawdown_pct", ".1f"),
            ("Avg Win ($)", "avg_win", ".2f"),
            ("Avg Loss ($)", "avg_loss", ".2f"),
            ("Sharpe", "sharpe", ".2f"),
            ("Sortino", "sortino", ".2f"),
            ("Calmar", "calmar", ".2f"),
            ("Max Consec Losses", "max_consec_losses", "d"),
        ]

        for name, key, fmt in metrics:
            line = f"  {name:<28s}"
            for v in variants:
                val = st[v][key]
                if fmt == "d":
                    line += f" {int(val):>19d}"
                elif fmt == ".1f":
                    line += f" {val:>19.1f}"
                elif fmt == ".2f":
                    if isinstance(val, float):
                        line += f" {val:>19.2f}"
                    else:
                        line += f" {str(val):>19s}"
                elif fmt == ",.0f":
                    line += f" ${val:>18,.0f}"
            print(line)

        for skip_key, skip_label in [
            ("skipped_cot", "COT blocked"),
            ("skipped_fvg", "No FVG"),
            ("skipped_trend", "Trend opposed"),
            ("skipped_fractal", "No fractal SL"),
            ("skipped_cooldown", "Cooldown"),
            ("skipped_be_wait", "BE wait"),
            ("skipped_trigger", "No H1 trigger"),
        ]:
            line = f"  [{skip_label:<26s}]"
            for v in variants:
                line += f" {int(st[v].get(skip_key, 0)):>19d}"
            print(line)

    # Combined
    print(f"\n  {'-'*86}")
    print(f"  COMBINED (XAU/USD + USD/JPY)")
    print(f"  {'-'*86}")
    header = f"  {'Metric':<28s}"
    for v in variants:
        header += f" {vlabels[v]:>19s}"
    print(header)
    print(f"  {'-'*86}")

    def _combined(variant, key):
        return sum(s[key] for s in all_stats if s["variant"] == variant)

    def _combined_wr(variant):
        w = sum(s["wins"] for s in all_stats if s["variant"] == variant)
        l = sum(s["losses"] for s in all_stats if s["variant"] == variant)
        return w/(w+l)*100 if (w+l)>0 else 0

    def _combined_pf(variant):
        wp = sum(s["avg_win"] * s["wins"] for s in all_stats if s["variant"] == variant)
        lp = sum(s["avg_loss"] * s["losses"] for s in all_stats if s["variant"] == variant)
        return wp/lp if lp > 0 else float("inf")

    for name, key, fmt in metrics:
        if name in ("Avg Win ($)", "Avg Loss ($)", "Sharpe", "Sortino", "Calmar", "Max Consec Losses"):
            continue
        line = f"  {name:<28s}"
        for v in variants:
            if name == "Win Rate":
                val = _combined_wr(v)
            elif name == "Profit Factor":
                val = _combined_pf(v)
            else:
                val = _combined(v, key)
            if fmt == "d":
                line += f" {int(val):>19d}"
            elif fmt in (".1f", ".2f"):
                line += f" {val:>19.1f}"
            elif fmt == ",.0f":
                line += f" ${val:>18,.0f}"
        print(line)

    print(f"\n  {'-'*86}")
    print(f"  VERDICT")
    print(f"  {'-'*86}")

    for v in variants:
        pnl = sum(s["total_pnl"] for s in all_stats if s["variant"] == v)
        trades = sum(s["total_trades"] for s in all_stats if s["variant"] == v)
        print(f"  {vlabels[v]:<24s} ${pnl:>+12,.0f}  ({trades} trades)")

    print()
    print("=" * 90)


if __name__ == "__main__":
    main()
