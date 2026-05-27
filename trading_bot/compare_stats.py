"""Compare 1/day vs 2/day full stats."""
import sys, os, io, pandas as pd
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')
sys.path.insert(0, '../cot_dashboard')
from backtest import *
import config

cot_signals = load_cot_history()
old_cooldown = config.MIN_BARS_BETWEEN_TRADES

for label, cooldown, max_pos in [('1/day (before)', 6, 99), ('2/day (after)', 3, 2)]:
    config.MIN_BARS_BETWEEN_TRADES = cooldown
    config.MAX_POSITIONS_PER_PAIR = max_pos

    all_trades = []
    for pair_name, symbol in config.PAIRS.items():
        trades, stats, bal = backtest_pair(symbol, pair_name, 10000, cot_signals,
                                            start_date='2017-01-01', end_date='2026-05-27',
                                            verbose=False)
        for t in trades:
            t['period'] = pair_name
        all_trades.extend(trades)

    df = pd.DataFrame(all_trades)
    closed = df[df['result'].isin(('win', 'loss', 'be'))]
    wins = closed[closed['result'] == 'win']
    losses = closed[closed['result'] == 'loss']
    bes = closed[closed['result'] == 'be']

    adv = compute_advanced_metrics(df, 20000)
    total_pnl = df['pnl'].sum()

    yearly = {}
    for yr in ['2017', '2018', '2019', '2020', '2021', '2022', '2023', '2024', '2025', '2026']:
        dy = df[df['time'].str.startswith(yr)]
        c = dy[dy['result'].isin(('win', 'loss', 'be'))]
        w = c[c['result'] == 'win']
        l = c[c['result'] == 'loss']
        wr_y = len(w) / (len(w) + len(l)) * 100 if len(w) + len(l) > 0 else 0
        yearly[yr] = {'pnl': dy['pnl'].sum(), 'trades': len(dy), 'wr': wr_y}

    pos_years = sum(1 for y in yearly.values() if y['pnl'] > 0)

    print(f'\n{"="*60}')
    print(f'  {label}')
    print(f'{"="*60}')
    print(f'  Trades: {len(df)} | Win: {len(wins)} | Loss: {len(losses)} | BE: {len(bes)}')
    print(f'  WR: {len(wins)/(len(wins)+len(losses))*100:.1f}%')
    print(f'  PnL: ${total_pnl:+,.0f} | Return: {total_pnl/20000*100:+.1f}%')
    print(f'  Max DD: {adv.get("max_drawdown_pct",0):.1f}%')
    print(f'  Sharpe: {adv["sharpe"]:.2f} | Sortino: {adv["sortino"]:.2f} | Calmar: {adv["calmar"]:.2f}')
    print(f'  Expectancy: ${adv["expectancy"]:+,.2f}')
    print(f'  Max Consec Loss: {adv["max_consec_losses"]}')
    print(f'  VaR 95%: ${adv["var_95"]:+,.0f} | CVaR: ${adv["cvar_95"]:+,.0f}')
    print(f'  Ruin Prob: {adv["ruin_prob"]:.1f}%')
    print(f'  MC Median: ${adv["mc_median_final"]:,.0f} | P10: ${adv["mc_p10_final"]:,.0f} | P90: ${adv["mc_p90_final"]:,.0f}')
    print(f'  Avg Hold: {adv["avg_hours_held"]:.0f}h')
    print(f'  Years Positive: {pos_years}/10')
    print(f'  Yearly PnL:')
    for yr, y in yearly.items():
        print(f'    {yr}: {y["trades"]:>4d} tr | WR:{y["wr"]:.0f}% | ${y["pnl"]:>+9,.0f}')
    print(f'  Per Pair:')
    for pair_name, symbol in config.PAIRS.items():
        pdf = df[df['period'] == pair_name]
        ppnl = pdf['pnl'].sum()
        pc = pdf[pdf['result'].isin(('win', 'loss', 'be'))]
        pw = pc[pc['result'] == 'win']
        pl = pc[pc['result'] == 'loss']
        pwr = len(pw) / (len(pw) + len(pl)) * 100 if len(pw) + len(pl) > 0 else 0
        print(f'    {pair_name}: {len(pdf)} tr | WR:{pwr:.1f}% | ${ppnl:+,.0f}')

config.MIN_BARS_BETWEEN_TRADES = old_cooldown
