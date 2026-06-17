"""Quick monthly P&L report: adx_tp vs adx_h1, 2023-2025, $100k."""
import sys, os, json, pandas as pd, numpy as np
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

def load_cot_signals():
    try:
        with open(os.path.join(os.path.dirname(__file__), "cot_history.json"), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except: return None
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
                inv = {"bullish": "bearish", "bearish": "bullish"}
                sig = inv.get(sig, sig); direction = inv.get(direction, direction)
            pair_signals.append({"date": rec["date"], "signal": sig, "score": score,
                                 "direction": direction, "text": f"COT {sig}"})
        signals[pair_key] = pair_signals
    return signals

def get_cot_at_date(cot_signals, pair_name, target_date):
    if cot_signals is None or pair_name not in cot_signals:
        return {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}
    s_list = cot_signals[pair_name]
    best = None
    for s in s_list:
        if s["date"] <= target_date: best = s
        else: break
    return best or {"signal": "neutral", "score": 0, "direction": "neutral", "text": "N/A"}

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
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100 * pd.Series(plus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr_arr
        minus_di = 100 * pd.Series(minus_dm).ewm(alpha=1/period, adjust=False).mean().values / atr_arr
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 0.0001)
    return float(pd.Series(dx).ewm(alpha=1/period, adjust=False).mean().values[-1])

def _find_recent_fractal(df_vis, direction):
    if len(df_vis) < 6: return None
    df_f = find_fractals(df_vis); recent = df_f.tail(5)
    if direction == "BUY":
        ups = recent[recent["up_fractal"].notna()]
        return float(ups["up_fractal"].iloc[-1]) if not ups.empty else None
    downs = recent[recent["down_fractal"].notna()]
    return float(downs["down_fractal"].iloc[-1]) if not downs.empty else None

def _make_trade(ap, exit_time, pair_name, result, exit_price, pnl, balance, bar_idx):
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
        "_bar_idx": bar_idx,
    }

def run_backtest(symbol, pair_name, strategy_mode, start_balance, cot_signals,
                 start_date, end_date):
    is_forex = pair_name in ("GBP/USD", "USD/JPY")
    rr = config.RISK_RR_FOREX if is_forex else config.RISK_RR
    use_h1 = (strategy_mode == "adx_h1")

    cache_dir = os.path.join(os.path.dirname(__file__), "_backtest_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = f"{cache_dir}/{symbol}_D_H4{'_H1' if use_h1 else ''}.pkl"

    cached = pd.read_pickle(cache_file)
    all_d1, all_h4, all_h1 = cached["d1"], cached["h4"], cached.get("h1")
    pt, cs, tv = cached["point"], cached["contract_size"], cached["tick_value"]

    if start_date:
        all_d1 = all_d1[all_d1["time"] >= pd.Timestamp(start_date)]
        all_h4 = all_h4[all_h4["time"] >= pd.Timestamp(start_date)]
        if all_h1 is not None: all_h1 = all_h1[all_h1["time"] >= pd.Timestamp(start_date)]
    if end_date:
        all_d1 = all_d1[all_d1["time"] <= pd.Timestamp(end_date)]
        all_h4 = all_h4[all_h4["time"] <= pd.Timestamp(end_date)]
        if all_h1 is not None: all_h1 = all_h1[all_h1["time"] <= pd.Timestamp(end_date)]
    all_d1, all_h4 = all_d1.reset_index(drop=True), all_h4.reset_index(drop=True)
    if all_h1 is not None: all_h1 = all_h1.reset_index(drop=True)

    balance = start_balance
    trades = []; active_positions = []; pending_signals = []

    for i in range(100, len(all_h4)):
        current_time = all_h4.iloc[i]["time"]

        still_open = []
        for ap in active_positions:
            if ap["entry_bar"] >= i: still_open.append(ap); continue
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
                    r = "be" if ap["be_triggered"] else "loss"
                    p = 0.0 if r == "be" else -ap["risk_amt"]
                    balance += p
                    trades.append(_make_trade(ap, current_time, pair_name, r,
                                               sl_check if r == "be" else ap["sl"], p, balance, i))
                    closed = True
                elif bar["high"] >= ap["tp"]:
                    rm = abs(ap["tp"] - ap["entry"]) / abs(ap["entry"] - ap["sl"])
                    p = ap["risk_amt"] * rm; balance += p
                    trades.append(_make_trade(ap, current_time, pair_name, "win", ap["tp"], p, balance, i))
                    closed = True
            else:
                if bar["high"] >= sl_check:
                    r = "be" if ap["be_triggered"] else "loss"
                    p = 0.0 if r == "be" else -ap["risk_amt"]
                    balance += p
                    trades.append(_make_trade(ap, current_time, pair_name, r,
                                               sl_check if r == "be" else ap["sl"], p, balance, i))
                    closed = True
                elif bar["low"] <= ap["tp"]:
                    rm = abs(ap["tp"] - ap["entry"]) / abs(ap["entry"] - ap["sl"])
                    p = ap["risk_amt"] * rm; balance += p
                    trades.append(_make_trade(ap, current_time, pair_name, "win", ap["tp"], p, balance, i))
                    closed = True
            if not closed: still_open.append(ap)
        active_positions = still_open

        if use_h1 and all_h1 is not None:
            still_pending = []
            for ps in pending_signals:
                if i - ps["h4_signal_bar"] >= 6: continue
                sht = all_h4.iloc[ps["h4_signal_bar"]]["time"]
                h1b = all_h1[(all_h1["time"] > sht) & (all_h1["time"] <= current_time)]
                trig_found = False; ehp = None; tt = ""
                for _, hb in h1b.iterrows():
                    h1c = all_h1.iloc[:hb.name + 1].tail(30)
                    h4c = all_h4.iloc[:i + 1].tail(5)
                    tg = check_h1_trigger(h1c, h4c, ps["direction"])
                    if tg["triggered"]:
                        ehp = hb["close"]; trig_found = True
                        tt = tg.get("trigger_type", "h1"); break
                if trig_found:
                    # Recalculate SL from ACTUAL entry price (mirrors main.py)
                    h4_vis_trigger = all_h4.iloc[:i + 1].tail(50)
                    atr_val_trig = calculate_atr(h4_vis_trigger, 14)
                    sl_new = nearest_fractal(h4_vis_trigger, ps["direction"].lower(), ehp,
                                             atr_value=atr_val_trig)
                    if sl_new is None:
                        sl_new = ps["sl"]  # fallback to original

                    # Recalculate TP from ACTUAL entry + new SL
                    regime = ps["regime"]
                    is_fx = "JPY" in pair_name or "GBP" in pair_name
                    if regime == "trend":
                        tp_mult = config.TP_ATR_MULT_FOREX if is_fx else config.TP_ATR_MULT
                        tp_new = ehp + atr_val_trig * tp_mult if ps["direction"] == "BUY" else ehp - atr_val_trig * tp_mult
                    elif regime == "range":
                        sl_dist_new = abs(ehp - sl_new)
                        tp_new = ehp + sl_dist_new * 1.0 if ps["direction"] == "BUY" else ehp - sl_dist_new * 1.0
                    else:
                        sl_dist_new = abs(ehp - sl_new)
                        rr_fb = config.RISK_RR_FOREX if is_fx else config.RISK_RR
                        tp_new = ehp + sl_dist_new * rr_fb if ps["direction"] == "BUY" else ehp - sl_dist_new * rr_fb

                    # Recalculate risk amount and volume with new SL
                    risk_amt_new = balance * (ps["risk_pct"] / 100)
                    sl_dist_new2 = abs(ehp - sl_new)
                    vol_new = risk_amt_new / (sl_dist_new2 / pt * tv) if tv and tv > 0 else risk_amt_new / (sl_dist_new2 * cs)
                    vol_new = round(max(0.01, vol_new), 2)

                    active_positions.append({
                        "entry_bar": i, "entry": ehp, "sl": sl_new, "tp": tp_new,
                        "direction": ps["direction"], "risk_amt": risk_amt_new,
                        "be_triggered": False, "current_sl": sl_new,
                        "risk_pct": ps["risk_pct"], "volume": vol_new,
                        "reason": ps["reason"] + " | H1 trig", "score": ps["score"],
                        "regime": regime, "trigger": tt})
                else: still_pending.append(ps)
            pending_signals = still_pending

        d1_trend = all_d1[all_d1["time"] <= current_time].tail(250)
        d1_fvg_data = all_d1[all_d1["time"] <= current_time].tail(20)
        h4_vis = all_h4.iloc[:i + 1].tail(50)
        if len(d1_fvg_data) < 10 or len(h4_vis) < 30: continue

        if active_positions:
            if len(active_positions) >= getattr(config, "MAX_POSITIONS_PER_PAIR", 2): continue
            if not all(p["be_triggered"] for p in active_positions): continue
            if (i - max(p["entry_bar"] for p in active_positions)) < config.MIN_BARS_BETWEEN_TRADES: continue
        elif trades:
            lc = max(t["_bar_idx"] for t in trades if t["pair"] == pair_name)
            if (i - lc) < config.MIN_BARS_BETWEEN_TRADES: continue

        fvg = check_fvg_signals(d1_fvg_data, h4_vis)
        if not fvg["direction"]: continue
        cot = get_cot_at_date(cot_signals, pair_name, str(current_time)[:10])
        trend = check_daily_trend(d1_trend, pair_name)
        signal = evaluate_setup(fvg, cot, trend)
        if not signal["trade"]: continue

        ep = all_h4.iloc[i]["close"]
        av = calculate_atr(h4_vis, 14)
        sl = nearest_fractal(h4_vis, signal["fvg_direction"], ep, atr_value=av)
        if sl is None: continue

        pos = calculate_lot(balance, ep, sl, signal["risk_pct"], pair_name, pt, cs, tv, rr=rr)
        if pos.get("error"): continue

        adx = _calc_adx(d1_trend, 14)
        trend_th, range_th = (20, 15) if is_forex else (25, 20)
        if adx is None: regime = "neutral"
        elif adx > trend_th: regime = "trend"
        elif adx < range_th: regime = "range"
        else: regime = "neutral"

        if regime == "trend":
            tm = config.TP_ATR_MULT_FOREX if is_forex else config.TP_ATR_MULT
            tp_price = ep + av * tm if signal["direction"] == "BUY" else ep - av * tm if av and av > 0 else pos["tp_price"]
        elif regime == "range":
            sd = abs(ep - sl)
            tp_price = ep + sd if signal["direction"] == "BUY" else ep - sd
        else: tp_price = pos["tp_price"]

        if strategy_mode == "adx_h1" and all_h1 is not None:
            ra = balance * (signal["risk_pct"] / 100)
            sd = abs(ep - sl)
            vol = ra / (sd / pt * tv) if tv and tv > 0 else ra / (sd * cs)
            pending_signals.append({"h4_signal_bar": i, "direction": signal["direction"],
                                    "sl": sl, "tp": tp_price, "risk_amt": ra,
                                    "risk_pct": signal["risk_pct"], "volume": round(max(0.01, vol), 2),
                                    "reason": signal["reason"], "score": signal.get("score", 0),
                                    "regime": regime})
        else:
            active_positions.append({"entry_bar": i, "entry": ep, "sl": sl, "tp": tp_price,
                                     "direction": signal["direction"], "risk_amt": pos["risk_amount"],
                                     "be_triggered": False, "current_sl": sl,
                                     "risk_pct": signal["risk_pct"], "volume": pos["volume"],
                                     "reason": signal["reason"], "score": signal.get("score", 0),
                                     "regime": regime, "trigger": "h4"})

    for ap in active_positions:
        trades.append(_make_trade(ap, all_h4.iloc[-1]["time"], pair_name,
                                   "be" if ap["be_triggered"] else "open",
                                   ap["entry"], 0.0, balance, ap["entry_bar"]))

    if not trades: return None, None
    df = pd.DataFrame(trades)
    df["exit_time_dt"] = pd.to_datetime(df["exit_time"])
    df["month"] = df["exit_time_dt"].dt.to_period("M")
    monthly = df.groupby("month")["pnl"].sum().sort_index()
    return df, monthly


# ===== MAIN =====
cot_signals = load_cot_signals()
pairs = [("GOLD.pro", "XAU/USD"), ("USDJPY.pro", "USD/JPY")]
MONTHS_RU = ["", "Янв", "Фев", "Мар", "Апр", "Май", "Июн",
             "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

for sm in ["adx_tp", "adx_h1"]:
    label = "ADX TP (H4 immediate)" if sm == "adx_tp" else "ADX TP + H1 TRIGGER"
    print(f"\n{'='*95}")
    print(f"  {label}  |  2016-2025  |  $100,000")
    print(f"{'='*95}")

    all_m = {}
    for sym, pn in pairs:
        df, monthly = run_backtest(sym, pn, sm, 100000, cot_signals, "2016-01-01", "2025-12-31")
        if monthly is not None: all_m[pn] = monthly

    all_months = sorted(set().union(*[set(m.index) for m in all_m.values()]))
    monthly_data = {}
    cum = 0; yearly = defaultdict(float)

    for m in all_months:
        xau = all_m.get("XAU/USD", pd.Series()).get(m, 0)
        uj = all_m.get("USD/JPY", pd.Series()).get(m, 0)
        total = xau + uj; cum += total
        yr = str(m).split("-")[0]; yearly[yr] += total
        monthly_data[m] = (xau, uj, total, cum)

    print(f"\n  {'Месяц':<7s} {'XAU/USD':>11s} {'USD/JPY':>11s} {'ИТОГО':>11s} {'НАКОП.':>13s}  {'График'}")
    print(f"  {'-'*78}")

    for m in all_months:
        xau, uj, total, cum = monthly_data[m]
        mn = int(str(m).split("-")[1])
        yr = str(m).split("-")[0][2:]
        mo_name = MONTHS_RU[mn]

        bar_len = min(15, int(abs(total) / 5000))
        bar = "#" * bar_len if total >= 0 else "!" * bar_len

        print(f"  {mo_name} '{yr:<3s} ${xau:>+9,.0f} ${uj:>+9,.0f} ${total:>+9,.0f} ${cum:>+11,.0f}  {bar}")

    # Yearly summary
    print(f"\n  {'ГОД':<7s} {'XAU/USD':>11s} {'USD/JPY':>11s} {'ИТОГО':>11s}")
    print(f"  {'-'*42}")
    total_xau = 0; total_uj = 0
    for yr in ["2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025"]:
        y_xau = sum(all_m.get("XAU/USD", pd.Series()).get(m, 0)
                    for m in all_months if str(m).startswith(yr))
        y_uj = sum(all_m.get("USD/JPY", pd.Series()).get(m, 0)
                   for m in all_months if str(m).startswith(yr))
        total_xau += y_xau; total_uj += y_uj
        print(f"  {yr:<7s} ${y_xau:>+9,.0f} ${y_uj:>+9,.0f} ${y_xau+y_uj:>+9,.0f}")
    print(f"  {'-'*42}")
    print(f"  {'TOTAL':<7s} ${total_xau:>+9,.0f} ${total_uj:>+9,.0f} ${total_xau+total_uj:>+9,.0f}")

    # Quarterly
    print(f"\n  Кварталы:")
    q_data = defaultdict(lambda: [0, 0])
    for m in all_months:
        yr = str(m).split("-")[0]; q = f"{yr}-Q{(int(str(m).split('-')[1])-1)//3+1}"
        q_data[q][0] += all_m.get("XAU/USD", pd.Series()).get(m, 0)
        q_data[q][1] += all_m.get("USD/JPY", pd.Series()).get(m, 0)
    for q in sorted(q_data):
        x, u = q_data[q]; t = x + u
        bar = "#" * min(20, max(1, int(abs(t) / 5000)))
        print(f"  {q}: XAU=${x:>+9,.0f}  UJ=${u:>+9,.0f}  TOTAL=${t:>+9,.0f}  {bar}")

print()
