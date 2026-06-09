"""Risk Protection — daily loss limit + total drawdown limit.

Checks before each trade: daily P&L <= -4%, total DD <= -8%.
State is module-level so main.py and web_server.py can both access it.
"""

from datetime import datetime


# --- State ---
day_start_balance = 0.0
peak_balance = 0.0
bot_paused = False
daily_stopped = False
_last_daily_reset = None


def update_peak(balance):
    """Track peak balance for max drawdown calculation."""
    global peak_balance
    if balance > peak_balance:
        peak_balance = balance


def check_limits(mt5, web_server, _tg):
    """Check daily loss (4%) and total DD (8%) limits.

    Returns (ok: bool, reason: str).
    Call before any trade entry.
    """
    global bot_paused, daily_stopped, day_start_balance, _last_daily_reset

    if bot_paused:
        return False, "bot_paused"

    now = datetime.now()
    today = now.date()

    # Reset daily counter at midnight
    if day_start_balance == 0 or _last_daily_reset != today:
        day_start_balance = 0
        _last_daily_reset = today
        daily_stopped = False

    try:
        mt5.connect()
        acc = mt5.get_account_summary()
        mt5.disconnect()
        balance = float(acc.get("balance", 0))
        equity = float(acc.get("equity", 0))
    except Exception:
        return True, "no_mt5"

    # Safety: if MT5 returns 0 (disconnected/broken), skip — never trigger limits
    if balance <= 0 or equity <= 0:
        return True, "no_mt5"
    # Guard against absurd balance change (stale/corrupt account data)
    if day_start_balance > 0 and abs(balance - day_start_balance) > day_start_balance * 0.5:
        return True, "no_mt5"

    # Init on first run
    if day_start_balance == 0:
        day_start_balance = balance
    update_peak(balance)

    # --- Daily loss check ---
    daily_pnl = equity - day_start_balance
    daily_loss_pct = abs(daily_pnl) / day_start_balance * 100 if daily_pnl < 0 else 0

    if daily_loss_pct >= 4.0 and not daily_stopped:
        daily_stopped = True
        print(f"  [LIMIT] DAILY LOSS -{daily_loss_pct:.1f}% — stop until tomorrow")
        web_server.add_log(f"<b>Дневной лимит -{daily_loss_pct:.1f}%</b> — стоп до завтра")
        _tg(f"🛑 *Дневной лимит* —{daily_loss_pct:.1f}% ($-{abs(daily_pnl):,.0f}) — бот остановлен до завтра")
        try:
            mt5.connect()
            mt5.close_all_positions()
            mt5.disconnect()
        except Exception:
            pass
        return False, "daily_loss"

    # --- Total drawdown check ---
    if peak_balance > 0:
        dd_pct = (peak_balance - equity) / peak_balance * 100
        if dd_pct >= 8.0:
            bot_paused = True
            print(f"  [LIMIT] TOTAL DD -{dd_pct:.1f}% — bot paused")
            web_server.add_log(f"<b>КРИТИЧЕСКАЯ ПРОСАДКА -{dd_pct:.1f}%</b> — бот остановлен")
            _tg(f"🚨 *КРИТИЧЕСКАЯ ПРОСАДКА* —{dd_pct:.1f}% (${(peak_balance-equity):,.0f}) — все позиции закрыты, планировщик на паузе")
            try:
                mt5.connect()
                mt5.close_all_positions()
                mt5.disconnect()
            except Exception:
                pass
            try:
                from main import sched
                if sched:
                    sched.pause()
            except Exception:
                pass
            return False, "total_dd"

    return True, "ok"


def init_limits(balance):
    """Initialize limits at startup with current balance."""
    global day_start_balance, peak_balance
    day_start_balance = balance
    peak_balance = balance


def resume():
    """Reset all limits and resume trading."""
    global bot_paused, daily_stopped, day_start_balance, peak_balance
    bot_paused = False
    daily_stopped = False
    day_start_balance = 0.0
    peak_balance = 0.0
