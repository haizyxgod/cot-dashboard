# Trading Bot Configuration
# Copy to .env and fill in your values
import os
from dotenv import load_dotenv

load_dotenv()

# --- OANDA ---
OANDA_API_KEY = os.getenv("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ENV = os.getenv("OANDA_ENV", "practice")  # "practice" or "live"

OANDA_URL = (
    "https://api-fxpractice.oanda.com/v3"
    if OANDA_ENV == "practice"
    else "https://api-fxtrade.oanda.com/v3"
)

# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- Trading Pairs ---
# OANDA instrument names
PAIRS = {
    "XAU/USD": "XAU_USD",
    "EUR/USD": "EUR_USD",
    "GBP/USD": "GBP_USD",
}

# --- Risk ---
RISK_RR = 1.7          # Take Profit ratio (1:X)
MAX_RISK_PCT = 2.0     # 2% per trade (full setup)
MID_RISK_PCT = 1.5     # 1.5% (partial setup)
LOW_RISK_PCT = 1.0     # 1% (minimal setup)
SIGNAL_TIMEOUT = 300   # 5 minutes to confirm

# --- Schedule ---
SCAN_INTERVAL_MINUTES = 180  # Every 3 hours
