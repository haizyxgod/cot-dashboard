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
    "EUR/USD": "EURUSD.pro",
}

# --- Risk ---
RISK_RR = 1.7                # Default (Gold)
RISK_RR_FOREX = 1.3          # EUR, GBP — lower volatility
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
MIN_BARS_BETWEEN_TRADES = 6  # H4 bars (24h) between trades on same pair
