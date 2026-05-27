"""Backtest v4: FVG-first + real COT filter + Trend + ATR SL.
Chain: FVG(D1+H4) -> Trend -> COT(CFTC) -> SL(ATR+H4 fractal)
COT blocks counter-FVG trades, boosts aligned score.
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys, os, random, json

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
# Real COT data loader
# ---------------------------------------------------------------------------

def load_cot_history():
    """Load historical COT data from JSON, pre-compute weekly signals."""
    path = os.path.join(os.path.dirname(__file__), "cot_history.json")
    if not os.path.exists(path):
        print("[WARN] cot_history.json not found — run download_cot_history.py first")
        return None

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Pre-compute COT signal per report date
    # For legacy (Gold): use speculative_net direction & change
    # For TFF (currencies): use leveraged_net direction & change
    cot_signals = {}  # pair_name -> list of {date, signal, score, direction, text}

    for pair_name, records in raw.items():
        if pair_name == "metadata":
            continue
        signals = []
        for i, rec in enumerate(records):
            if "speculative_net" in rec:
                net = rec["speculative_net"]
            else:
                net = rec.get("leveraged_net", 0)

            prev_net = 0
            if i > 0:
                prev_rec = records[i - 1]
                prev_net = prev_rec.get("speculative_net") or prev_rec.get("leveraged_net", 0)

            net_change = net - prev_net

            # Determine signal
            if net > 0 and net_change > 0:
                sig = "strong_bullish"; score = 80
            elif net > 0 and net_change <= 0:
                sig = "bullish"; score = 60
            elif net < 0 and net_change < 0:
                sig = "strong_bearish"; score = 80
            elif net < 0 and net_change >= 0:
                sig = "bearish"; score = 60
            else:
                sig = "neutral"; score = 0

            direction = "bullish" if "bull" in sig else "bearish" if "bear" in sig else "neutral"

            # JPY pairs: COT tracks JPY futures → invert
            # leveraged_net > 0 = long JPY = BEARISH USD/JPY
            if "JPY" in pair_name:
                if direction == "bullish":
                    direction = "bearish"
                    sig = "strong_bearish" if "strong" in sig else "bearish"
                elif direction == "bearish":
                    direction = "bullish"
                    sig = "strong_bullish" if "strong" in sig else "bullish"

            signals.append({
                "date": rec["date"],
                "signal": sig,
                "score": score,
                "direction": direction,
                "text": f"COT {sig} (net={net:+,})",
            })
        cot_signals[pair_name] = signals

    print(f"[COT] Loaded history: { {k: len(v) for k, v in cot_signals.items()} }")
    return cot_signals


def get_cot_at_date(cot_signals, pair_name, target_date):
    """Get the most recent COT signal before target_date."""
    if cot_signals is None or pair_name not in cot_signals:
        return {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}

    signals = cot_signals[pair_name]
    # Binary search for nearest report date <= target_date
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
# Outcome simulation
# ---------------------------------------------------------------------------

def simulate_outcome(df_h4, entry_idx, entry, sl, tp, direction):
    """Simulate trade outcome with BE on H4 fractal breakout.

    When price crosses a new H4 fractal in the trade direction:
    SL moves to entry (break-even).
    """
    be_triggered = False
    be_bar = None

    for j in range(entry_idx + 1, len(df_h4)):
        bar = df_h4.iloc[j]
        bars_held = j - entry_idx
        current_sl = entry if be_triggered else sl

        # Check for BE trigger: new H4 fractal beyond entry in trade direction
        if not be_triggered and j >= entry_idx + 3:
            # Get visible data up to this bar
            vis = df_h4.iloc[:j + 1]
            recent_fractal = _find_recent_fractal(vis, direction)
            if recent_fractal is not None:
                if direction == "BUY" and recent_fractal > entry:
                    be_triggered = True
                    be_bar = j
                    current_sl = entry
                elif direction == "SELL" and recent_fractal < entry:
                    be_triggered = True
                    be_bar = j
                    current_sl = entry

        # Check exit
        if direction == "BUY":
            if bar["low"] <= current_sl:
                if be_triggered:
                    return {"result": "be", "exit_price": entry,
                            "exit_time": bar["time"], "bars_held": bars_held,
                            "be_bar": be_bar}
                return {"result": "loss", "exit_price": sl,
                        "exit_time": bar["time"], "bars_held": bars_held}
            if bar["high"] >= tp:
                return {"result": "win", "exit_price": tp,
                        "exit_time": bar["time"], "bars_held": bars_held,
                        "be_triggered": be_triggered}
        else:  # SELL
            if bar["high"] >= current_sl:
                if be_triggered:
                    return {"result": "be", "exit_price": entry,
                            "exit_time": bar["time"], "bars_held": bars_held,
                            "be_bar": be_bar}
                return {"result": "loss", "exit_price": sl,
                        "exit_time": bar["time"], "bars_held": bars_held}
            if bar["low"] <= tp:
                return {"result": "win", "exit_price": tp,
                        "exit_time": bar["time"], "bars_held": bars_held,
                        "be_triggered": be_triggered}

    return {"result": "open", "exit_price": None, "exit_time": None,
            "bars_held": None, "be_triggered": be_triggered}


def _find_recent_fractal(df_vis, direction):
    """Find the most recent H4 fractal in the given direction."""
    if len(df_vis) < 7:
        return None
    df_f = find_fractals(df_vis)
    recent = df_f.tail(5)
    if direction == "BUY":
        ups = recent[recent["up_fractal"].notna()]
        return ups["up_fractal"].iloc[-1] if not ups.empty else None
    else:
        downs = recent[recent["down_fractal"].notna()]
        return downs["down_fractal"].iloc[-1] if not downs.empty else None


# ---------------------------------------------------------------------------
# Advanced metrics
# ---------------------------------------------------------------------------

def compute_advanced_metrics(df, start_balance, risk_free_annual=0.03):
    closed = df[df["result"].isin(("win", "loss"))].copy()
    if closed.empty:
        return {}

    pnl = closed["pnl"].values
    n = len(pnl)
    expectancy = pnl.mean()

    streak = 0; max_streak = 0
    for r in closed["result"]:
        if r == "loss":
            streak += 1; max_streak = max(max_streak, streak)
        else:
            streak = 0

    avg_bars = closed["bars_held"].mean() if "bars_held" in closed.columns else None

    closed = closed.copy()
    closed["time_dt"] = pd.to_datetime(closed["time"])
    closed = closed.sort_values("time_dt")
    closed["cum_pnl"] = closed["pnl"].cumsum()
    closed["balance"] = start_balance + closed["cum_pnl"]

    daily = closed.set_index("time_dt").resample("D")["balance"].last().ffill()
    daily_returns = daily.pct_change().dropna()
    ann_factor = np.sqrt(252) if len(daily_returns) > 1 else 0

    mean_daily = daily_returns.mean()
    std_daily = daily_returns.std()
    sharpe = ((mean_daily - risk_free_annual / 252) / std_daily * ann_factor) if std_daily > 0 else 0

    downside = daily_returns[daily_returns < 0]
    downside_std = downside.std() if len(downside) > 1 else std_daily
    sortino = ((mean_daily - risk_free_annual / 252) / downside_std * ann_factor) if downside_std > 0 else 0

    peak = start_balance; max_dd = 0.0
    for bal in closed["balance"]:
        if bal > peak: peak = bal
        dd = (peak - bal) / peak
        if dd > max_dd: max_dd = dd
    total_return = (closed["balance"].iloc[-1] - start_balance) / start_balance
    calmar = total_return / max_dd if max_dd > 0 else 0

    mc = monte_carlo(pnl, start_balance, n_sims=10_000)

    return {
        "expectancy": expectancy,
        "max_consec_losses": max_streak,
        "avg_bars_held": avg_bars,
        "avg_hours_held": (avg_bars * 4) if avg_bars else None,
        "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "var_95": mc["var_95"], "cvar_95": mc["cvar_95"],
        "ruin_prob": mc["ruin_prob"],
        "mc_median_final": mc["median_final"],
        "mc_p10_final": mc["p10_final"], "mc_p90_final": mc["p90_final"],
    }


def monte_carlo(trade_pnls, start_balance, n_sims=10000):
    rng = np.random.RandomState(42)
    finals = []
    for _ in range(n_sims):
        seq = rng.choice(trade_pnls, size=len(trade_pnls), replace=True)
        finals.append(start_balance + seq.sum())
    finals = np.array(finals); finals.sort()
    var_95 = start_balance - np.percentile(finals, 5)
    tail = finals[finals <= np.percentile(finals, 5)]
    cvar_95 = start_balance - tail.mean() if len(tail) > 0 else var_95
    ruin_prob = (finals < start_balance * 0.5).mean() * 100
    return {
        "var_95": var_95, "cvar_95": cvar_95, "ruin_prob": ruin_prob,
        "median_final": np.median(finals),
        "p10_final": np.percentile(finals, 10),
        "p90_final": np.percentile(finals, 90),
    }


def _calc_adx(df, period=14):
    """Calculate ADX(14) — trend strength indicator."""
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

    # Wilder's smoothing
    atr = pd.Series(tr).ewm(alpha=1/period, adjust=False).mean().values
    plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr
    minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 0.0001)
    adx = pd.Series(dx).ewm(alpha=1/period, adjust=False).mean().values

    return float(adx[-1])


def _make_trade(ap, exit_time, pair_name, result, exit_price, pnl, balance, bar_idx):
    return {
        "time": str(exit_time)[:19],
        "pair": pair_name,
        "direction": ap["direction"],
        "entry": round(ap["entry"], 6),
        "sl": round(ap["sl"], 6),
        "tp": round(ap["tp"], 6),
        "risk_pct": ap.get("risk_pct", 0),
        "volume": ap.get("volume", 0),
        "risk_amt": ap.get("risk_amt", 0),
        "result": result,
        "exit_price": round(exit_price, 6) if exit_price else None,
        "exit_time": str(exit_time)[:19],
        "bars_held": bar_idx - ap["entry_bar"],
        "pnl": round(pnl, 2),
        "balance": round(balance, 2),
        "reason": ap.get("reason", ""),
        "score": ap.get("score", 0),
        "regime": ap.get("regime", "neutral"),
        "_bar_idx": bar_idx,
    }


# ---------------------------------------------------------------------------
# Single-pair backtest
# ---------------------------------------------------------------------------

def backtest_pair(symbol, pair_name, start_balance=10000, cot_signals=None,
                  start_date=None, end_date=None, verbose=True):
    is_forex = pair_name in ("EUR/USD", "GBP/USD", "USD/JPY")
    rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR

    if verbose:
        print(f"\n{'='*55}")
        print(f"  BACKTEST: {pair_name} ({symbol})")
        print(f"  Chain: FVG(D1+H4) -> Trend -> COT(CFTC filter) -> SL(H4+ATR)")
        print(f"  RR: {rr}  |  Start balance: ${start_balance:,.0f}")
        print(f"{'='*55}")

    mt5.connect()
    all_d1 = mt5.get_candles(symbol, "D", 5000)
    all_h4 = mt5.get_candles(symbol, "H4", 40000)
    info = mt5.get_symbol_info(symbol)
    mt5.disconnect()

    if all_d1.empty or all_h4.empty:
        print(f"  [SKIP] No data for {symbol}")
        return [], {}, start_balance

    pt = info["point"] if info else (0.00001 if "EUR" in symbol or "GBP" in symbol else 0.01)
    cs = info["trade_contract_size"] if info else (100000 if "EUR" in symbol or "GBP" in symbol else 100)
    tv = info["trade_tick_value"] if info else 0

    all_d1 = all_d1.sort_values("time").reset_index(drop=True)
    all_h4 = all_h4.sort_values("time").reset_index(drop=True)

    # Filter by date range if specified
    if start_date:
        all_d1 = all_d1[all_d1["time"] >= pd.Timestamp(start_date)]
        all_h4 = all_h4[all_h4["time"] >= pd.Timestamp(start_date)]
    if end_date:
        all_d1 = all_d1[all_d1["time"] <= pd.Timestamp(end_date)]
        all_h4 = all_h4[all_h4["time"] <= pd.Timestamp(end_date)]

    all_d1 = all_d1.reset_index(drop=True)
    all_h4 = all_h4.reset_index(drop=True)

    if verbose:
        print(f"  D1: {len(all_d1)} bars | H4: {len(all_h4)} bars")
        print(f"  Range: {all_h4['time'].iloc[0]} --> {all_h4['time'].iloc[-1]}")
        print(f"  Point={pt:.6f}  ContractSize={cs}  TickValue={tv}")

    balance = start_balance
    trades = []
    active_positions = []  # list of {entry_bar, entry, sl, tp, direction, be_triggered}

    skip_cot = 0; skip_fvg = 0; skip_trend = 0
    skip_fractal = 0; skip_cooldown = 0; skip_no_trade = 0
    skip_be_wait = 0; skip_session = 0

    for i in range(100, len(all_h4)):
        current_time = all_h4.iloc[i]["time"]

        # --- First: process all active positions up to this bar ---
        still_open = []
        for ap in active_positions:
            if ap["entry_bar"] >= i:
                still_open.append(ap)
                continue
            # Check BE trigger: H4 fractal breakout beyond entry
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

        # --- Entry gate: cooldown (first position) or BE check (add to existing) ---
        d1_trend = all_d1[all_d1["time"] <= current_time].tail(250)
        d1_fvg_data = all_d1[all_d1["time"] <= current_time].tail(20)
        h4_vis = all_h4.iloc[:i + 1].tail(50)

        if len(d1_fvg_data) < 10 or len(h4_vis) < 30:
            continue

        if active_positions:
            if len(active_positions) >= getattr(config, 'MAX_POSITIONS_PER_PAIR', 2):
                skip_be_wait += 1
                continue
            # Check if all positions are at BE
            all_be = all(p["be_triggered"] for p in active_positions)
            if not all_be:
                skip_be_wait += 1
                continue
            # All at BE — allow adding, but enforce cooldown from last entry
            last_entry = max(p["entry_bar"] for p in active_positions)
            if (i - last_entry) < config.MIN_BARS_BETWEEN_TRADES:
                skip_cooldown += 1
                continue
        else:
            # No positions — normal cooldown from last trade
            if trades:
                last_close = max(t["_bar_idx"] for t in trades if t["pair"] == pair_name)
                if (i - last_close) < config.MIN_BARS_BETWEEN_TRADES:
                    skip_cooldown += 1
                    continue

        # --- Signal evaluation ---
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

        # --- Market regime detection via ADX(14) on D1 ---
        adx = _calc_adx(d1_trend, 14)
        default_rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR

        if is_forex:
            trend_thresh, range_thresh = 20, 15  # EUR: lower ADX thresholds
        else:
            trend_thresh, range_thresh = 25, 20  # Gold

        if adx is None:
            regime = "neutral"
        elif adx > trend_thresh:
            regime = "trend"
        elif adx < range_thresh:
            regime = "range"
        else:
            regime = "neutral"

        # Dynamic TP based on regime
        if regime == "trend":
            # ATR-based TP (wider in trends)
            tp_mult = config.TP_ATR_MULT_FOREX if is_forex else config.TP_ATR_MULT
            if atr_val and atr_val > 0:
                if signal["direction"] == "BUY":
                    tp_price = entry_price + atr_val * tp_mult
                else:
                    tp_price = entry_price - atr_val * tp_mult
            else:
                tp_price = pos["tp_price"]
        elif regime == "range":
            # 1:1 RR — quick scalp
            sl_dist = abs(entry_price - sl)
            if signal["direction"] == "BUY":
                tp_price = entry_price + sl_dist * 1.0
            else:
                tp_price = entry_price - sl_dist * 1.0
        else:
            # Neutral — default fixed RR
            tp_price = pos["tp_price"]

        active_positions.append({
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
            "regime": regime,
        })

    # Close any remaining open positions at end of data
    for ap in active_positions:
        result = "be" if ap["be_triggered"] else "open"
        pnl = 0.0
        trades.append(_make_trade(ap, all_h4.iloc[-1]["time"], pair_name, result,
                                   ap["entry"], pnl, balance, ap["entry_bar"]))

    if not trades:
        print(f"\n  [NO TRADES] COT={skip_cot} FVG={skip_fvg} Trend={skip_trend} "
              f"NoTrade={skip_no_trade} Fractal={skip_fractal} Cooldown={skip_cooldown}")
        return [], {}, start_balance

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
    opens_n = len(opens)
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

    adv = compute_advanced_metrics(df, start_balance)

    stats = {
        "pair": pair_name, "symbol": symbol,
        "start_balance": start_balance,
        "final_balance": round(final_balance, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((final_balance - start_balance) / start_balance * 100, 2),
        "total_trades": total_trades, "wins": wins_n, "losses": losses_n,
        "be": bes_n, "open": opens_n, "win_rate": round(wr, 1),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "max_drawdown_pct": round(max_dd_pct, 2),
        "best_trade": round(df["pnl"].max(), 2),
        "worst_trade": round(df["pnl"].min(), 2),
        "cot_blocked": skip_cot,
        **adv,
    }

    if verbose:
        print(f"\n  +{'-'*52}+")
        print(f"  | {'TRADE SUMMARY':^50s} |")
        print(f"  +{'-'*52}+")
        print(f"  | {'TOTAL TRADES:':<28s} {total_trades:>5d}  (open {opens_n})    |")
        print(f"  | {'WINS / LOSS / BE:':<28s} {wins_n:>4d} / {losses_n:<4d} / {bes_n:<4d} (WR {wr:.1f}%)   |")
        print(f"  +{'-'*52}+")
        print(f"  | {'TOTAL P&L:':<28s} ${total_pnl:>+10,.2f}           |")
        print(f"  | {'RETURN:':<28s} {stats['total_return_pct']:>+10.1f}%           |")
        print(f"  | {'MAX DRAWDOWN:':<28s} {max_dd_pct:>10.1f}%           |")
        print(f"  | {'PROFIT FACTOR:':<28s} {str(stats['profit_factor']):>10s}           |")
        print(f"  | {'EXPECTANCY / trade:':<28s} ${adv.get('expectancy', 0):>+10.2f}           |")
        print(f"  +{'-'*52}+")
        print(f"  | {'SHARPE:':<28s} {adv.get('sharpe', 0):>10.2f}           |")
        print(f"  | {'SORTINO:':<28s} {adv.get('sortino', 0):>10.2f}           |")
        print(f"  | {'CALMAR:':<28s} {adv.get('calmar', 0):>10.2f}           |")
        print(f"  +{'-'*52}+")
        print(f"  | {'MC (10k): VAR 95%':<28s} ${adv.get('var_95', 0):>10,.0f}           |")
        print(f"  | {'RUIN PROB':<28s} {adv.get('ruin_prob', 0):>9.1f}%           |")
        print(f"  | {'MC MEDIAN':<28s} ${adv.get('mc_median_final', 0):>10,.0f}           |")
        print(f"  +{'-'*52}+")
        print(f"  | {'MAX CONSEC LOSSES:':<28s} {adv.get('max_consec_losses', 0):>10d}           |")
        hrs = adv.get("avg_hours_held", 0)
        print(f"  | {'AVG HOLD:':<28s} {adv.get('avg_bars_held', 0) or 0:>7.1f} bars ({hrs or 0:.0f}h)     |")
        print(f"  +{'-'*52}+")
        print(f"  Skipped: COT={skip_cot} FVG={skip_fvg} Trend={skip_trend} "
              f"NoTrade={skip_no_trade} Fractal={skip_fractal} "
              f"Cooldown={skip_cooldown} BE-wait={skip_be_wait} Session={skip_session}")

    return trades, stats, final_balance


# ---------------------------------------------------------------------------
# H1 Backtest variant
# ---------------------------------------------------------------------------

def backtest_pair_h1(symbol, pair_name, start_balance=10000, cot_signals=None,
                     start_date=None, end_date=None, verbose=True):
    """H1 version — same strategy, faster timeframe."""
    is_forex = pair_name in ("EUR/USD", "GBP/USD", "USD/JPY")
    rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR
    cooldown_bars = 24  # 24h on H1

    if verbose:
        print(f"\n{'='*55}")
        print(f"  BACKTEST H1: {pair_name} ({symbol})")
        print(f"  RR: {rr}  |  Cooldown: {cooldown_bars} bars (24h)")
        print(f"{'='*55}")

    mt5.connect()
    all_d1 = mt5.get_candles(symbol, "D", 3000)
    all_h1 = mt5.get_candles(symbol, "H1", 50000)
    info = mt5.get_symbol_info(symbol)
    mt5.disconnect()

    if all_d1.empty or all_h1.empty:
        print(f"  [SKIP] No data")
        return [], {}, start_balance

    pt = info["point"] if info else (0.001 if "JPY" in symbol else 0.00001 if is_forex else 0.01)
    cs = info["trade_contract_size"] if info else (100000 if is_forex else 100)
    tv = info["trade_tick_value"] if info else 0

    all_d1 = all_d1.sort_values("time").reset_index(drop=True)
    all_h1 = all_h1.sort_values("time").reset_index(drop=True)

    if start_date:
        all_d1 = all_d1[all_d1["time"] >= pd.Timestamp(start_date)]
        all_h1 = all_h1[all_h1["time"] >= pd.Timestamp(start_date)]
    if end_date:
        all_d1 = all_d1[all_d1["time"] <= pd.Timestamp(end_date)]
        all_h1 = all_h1[all_h1["time"] <= pd.Timestamp(end_date)]
    all_d1 = all_d1.reset_index(drop=True)
    all_h1 = all_h1.reset_index(drop=True)

    if verbose:
        print(f"  D1: {len(all_d1)} | H1: {len(all_h1)} bars")

    balance = start_balance
    trades = []
    active_positions = []

    skip_cot = 0; skip_fvg = 0; skip_trend = 0
    skip_fractal = 0; skip_cooldown = 0; skip_no_trade = 0
    skip_be_wait = 0

    for i in range(200, len(all_h1)):
        current_time = all_h1.iloc[i]["time"]

        # --- Process active positions ---
        still_open = []
        for ap in active_positions:
            if ap["entry_bar"] >= i:
                still_open.append(ap)
                continue
            if not ap["be_triggered"]:
                vis_be = all_h1.iloc[:i + 1]
                frac = _find_recent_fractal(vis_be, ap["direction"])
                if frac is not None:
                    if ap["direction"] == "BUY" and frac > ap["entry"]:
                        ap["be_triggered"] = True
                        ap["current_sl"] = ap["entry"]
                    elif ap["direction"] == "SELL" and frac < ap["entry"]:
                        ap["be_triggered"] = True
                        ap["current_sl"] = ap["entry"]

            sl_check = ap["entry"] if ap["be_triggered"] else ap["sl"]
            bar = all_h1.iloc[i]
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
                    pnl = ap["risk_amt"] * r_mult
                    balance += pnl
                    trades.append(_make_trade(ap, current_time, pair_name, "win",
                                               ap["tp"], pnl, balance, i))
                    closed = True
            if not closed:
                still_open.append(ap)
        active_positions = still_open

        # --- Entry gates ---
        d1_trend = all_d1[all_d1["time"] <= current_time].tail(250)
        d1_fvg_data = all_d1[all_d1["time"] <= current_time].tail(20)
        h1_vis = all_h1.iloc[:i + 1].tail(100)

        if len(d1_fvg_data) < 10 or len(h1_vis) < 50:
            continue

        if active_positions:
            if len(active_positions) >= getattr(config, 'MAX_POSITIONS_PER_PAIR', 2):
                skip_be_wait += 1
                continue
            all_be = all(p["be_triggered"] for p in active_positions)
            if not all_be:
                skip_be_wait += 1
                continue
            last_entry = max(p["entry_bar"] for p in active_positions)
            if (i - last_entry) < cooldown_bars:
                skip_cooldown += 1
                continue
        else:
            if trades:
                last_close = max(t["_bar_idx"] for t in trades if t["pair"] == pair_name)
                if (i - last_close) < cooldown_bars:
                    skip_cooldown += 1
                    continue

        # --- FVG on D1+H1 ---
        # Build H4 candles from H1 for the FVG check (H4 = 4xH1 bars)
        h4_vis = _h1_to_h4(h1_vis)
        fvg = check_fvg_signals(d1_fvg_data, h4_vis)
        if not fvg["direction"]:
            # Also try pure H1 FVG
            fvg_h1 = check_fvg_signals(d1_fvg_data, h1_vis)
            if not fvg_h1["direction"]:
                skip_fvg += 1
                continue
            fvg = fvg_h1

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

        entry_price = all_h1.iloc[i]["close"]

        # ADX regime (D1)
        adx = _calc_adx(d1_trend, 14)
        if is_forex:
            trend_thresh, range_thresh = 20, 15
        else:
            trend_thresh, range_thresh = 25, 20

        if adx is None: regime = "neutral"
        elif adx > trend_thresh: regime = "trend"
        elif adx < range_thresh: regime = "range"
        else: regime = "neutral"

        # SL: H1 fractal + H1 ATR
        atr_val = calculate_atr(h1_vis, 14)
        sl = nearest_fractal(h1_vis, signal["fvg_direction"], entry_price,
                             atr_value=atr_val)
        if sl is None:
            skip_fractal += 1
            continue

        pos = calculate_lot(balance, entry_price, sl, signal["risk_pct"],
                            pair_name, pt, cs, tv, rr=rr)
        if pos.get("error"):
            continue

        # TP
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

        active_positions.append({
            "entry_bar": i, "entry": entry_price, "sl": sl, "tp": tp_price,
            "direction": signal["direction"], "risk_amt": pos["risk_amount"],
            "be_triggered": False, "current_sl": sl,
            "risk_pct": signal["risk_pct"], "volume": pos["volume"],
            "reason": signal["reason"], "score": signal.get("score", 0),
            "regime": regime,
        })

    for ap in active_positions:
        result = "be" if ap["be_triggered"] else "open"
        pnl = 0.0
        trades.append(_make_trade(ap, all_h1.iloc[-1]["time"], pair_name, result,
                                   ap["entry"], pnl, balance, ap["entry_bar"]))

    if not trades:
        return [], {}, start_balance

    df = pd.DataFrame(trades)
    closed = df[df["result"].isin(("win", "loss", "be"))]
    wins = closed[closed["result"] == "win"]
    losses = closed[closed["result"] == "loss"]
    bes = closed[closed["result"] == "be"]
    opens = df[df["result"] == "open"]

    total_trades = len(df)
    wins_n = len(wins); losses_n = len(losses); bes_n = len(bes); opens_n = len(opens)
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

    adv = compute_advanced_metrics(df, start_balance)

    stats = {
        "pair": pair_name, "symbol": symbol,
        "start_balance": start_balance,
        "final_balance": round(final_balance, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round((final_balance - start_balance) / start_balance * 100, 2),
        "total_trades": total_trades, "wins": wins_n, "losses": losses_n,
        "be": bes_n, "open": opens_n, "win_rate": round(wr, 1),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "max_drawdown_pct": round(max_dd_pct, 2),
        "best_trade": round(df["pnl"].max(), 2),
        "worst_trade": round(df["pnl"].min(), 2),
        "cot_blocked": skip_cot,
        **adv,
    }

    if verbose:
        print(f"  Trades: {total_trades} | Win:{wins_n} Loss:{losses_n} BE:{bes_n} | WR:{wr:.1f}%")
        print(f"  P&L: ${total_pnl:+,.2f} | DD: {max_dd_pct:.1f}% | Sharpe: {adv.get('sharpe',0):.2f}")
        print(f"  Skipped: COT={skip_cot} FVG={skip_fvg} Trend={skip_trend} "
              f"NoTrade={skip_no_trade} Fractal={skip_fractal} "
              f"Cooldown={skip_cooldown} BE-wait={skip_be_wait}")

    return trades, stats, final_balance


def _h1_to_h4(df_h1):
    """Resample H1 to H4 candles for FVG check."""
    if df_h1.empty:
        return df_h1
    df = df_h1.copy()
    df["time"] = pd.to_datetime(df["time"])
    df = df.set_index("time")
    h4 = df.resample("4h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna()
    return h4.reset_index().rename(columns={"index": "time"})


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
    cot_signals = load_cot_history()

    for period_name, start, end in PERIODS:
        print("\n" + "=" * 55)
        print(f"  PERIOD: {period_name}")
        print(f"  FVG-first + COT(CFTC) filter")
        print(f"  Gold RR={config.RISK_RR} | Forex RR={config.RISK_RR_FOREX}")
        print("=" * 55)

        all_trades = []; all_stats = []

        for pair_name, symbol in config.PAIRS.items():
            trades, stats, bal = backtest_pair(symbol, pair_name, start_balance=10000,
                                                cot_signals=cot_signals,
                                                start_date=start, end_date=end,
                                                verbose=False)
            all_trades.extend(trades)
            all_stats.append(stats)

        if not all_trades:
            print(f"\n[NO TRADES] Period {period_name}")
            continue

        df_all = pd.DataFrame(all_trades)
        closed = df_all[df_all["result"].isin(("win", "loss"))]
        wins = closed[closed["result"] == "win"]
        losses = closed[closed["result"] == "loss"]

        total_trades = len(df_all)
        wins_n = len(wins); losses_n = len(losses)
        wr = wins_n / (wins_n + losses_n) * 100 if (wins_n + losses_n) > 0 else 0
        total_pnl = df_all["pnl"].sum()
        pf = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses_n > 0 and losses["pnl"].sum() != 0 else float("inf")

        print(f"\n  --- {period_name} ---")
        header = f"  {'Pair':<10s} {'Trades':>6s} {'Win':>4s} {'Loss':>5s} {'BE':>5s} {'WR':>6s} {'P&L':>10s} {'DD':>7s} {'Sharpe':>7s}"
        print(header)
        print("  " + "-" * 65)
        for s in all_stats:
            if s:
                print(f"  {s['pair']:<10s} {s['total_trades']:>6d} {s.get('wins',0):>4d} "
                      f"{s.get('losses',0):>5d} {s.get('be',0):>5d} {s.get('win_rate',0):>5.1f}% "
                      f"${s.get('total_pnl',0):>+9,.2f} {s.get('max_drawdown_pct',0):>6.1f}% "
                      f"{s.get('sharpe',0):>7.2f}")
        print(f"  {'TOTAL':<10s} {total_trades:>6d} {wins_n:>4d} {losses_n:>5d} "
              f"{total_trades - wins_n - losses_n - (len(df_all) - len(closed)):>5d} {wr:>5.1f}% "
              f"${total_pnl:>+9,.2f}")
        all_trades.extend(trades)
        all_stats.append(stats)



if __name__ == "__main__":
    main()
