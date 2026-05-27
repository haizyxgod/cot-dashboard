"""Trading Bot Configuration — MT5 + Telegram."""
import os
from dotenv import load_dotenv

load_dotenv()

# --- MT5 ---
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "OANDATMS-MT5")

# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Trading Pairs ---
# MT5 symbol names
PAIRS = {
    "XAU/USD": "GOLD.pro",
    "USD/JPY": "USDJPY.pro",
}

# --- Risk ---
RISK_RR = 1.7                # Gold — fixed RR (fallback)
RISK_RR_FOREX = 1.3          # EUR — fixed RR (fallback)
# Dynamic TP via ATR (overrides RR when enabled)
TP_ATR_MULT = 2.0            # Gold: TP = entry + ATR(14) * multiplier
TP_ATR_MULT_FOREX = 1.5      # EUR
HIGH_RISK_PCT = 2.0          # 3-4 variables aligned
MID_RISK_PCT = 1.0           # 2 variables
LOW_RISK_PCT = 0.5           # 1 variable (uncertainty)
SIGNAL_TIMEOUT = 300

# --- Entry trigger (H1) ---
H1_ATR_SURGE_MULT = 1.5   # H1 bar range > 1.5x avg range = volatility trigger
TRIGGER_MAX_BARS = 24     # Max H1 bars to wait for trigger (24h)
TRIGGER_SCAN_SEC = 900    # Re-check trigger every 15 min

# --- Schedule ---
SCAN_INTERVAL_MINUTES = 180  # Every 3 hours (COT+D1 check)
MIN_BARS_BETWEEN_TRADES = 3  # H4 bars (12h)
MAX_POSITIONS_PER_PAIR = 2   # max concurrent per pair
