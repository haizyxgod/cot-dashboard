"""COT + FVG Trading Bot — Main Entry Point.

Сканирует пары каждые 3 часа:
1. Получает свечи D1, H4 с OANDA
2. Ищет FVG на D1 и H4
3. Сверяет с COT-вердиктом
4. Находит фрактальный SL на H4
5. Отправляет сигнал в Telegram с кнопками
"""

import sys
import os
import time
from datetime import datetime

# Добавляем путь к cot_dashboard для импорта cot_fetcher
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cot_dashboard"))

from apscheduler.schedulers.background import BackgroundScheduler
from oanda_client import OandaClient
from fvg_detector import check_fvg_signals
from fractal_detector import nearest_fractal
from signal_engine import evaluate_setup
from risk_manager import calculate_position
from telegram_bot import send_signal, send_text
import config

client = OandaClient()

# --- COT Engine ---
# Используем готовый cot_fetcher из дашборда
try:
    from cot_fetcher import COTDataFetcher

    cot_fetcher = COTDataFetcher()
    COT_AVAILABLE = True
    print("[OK] COT engine loaded")
except ImportError:
    COT_AVAILABLE = False
    print("[WARN] COT engine not available — trading without COT filter")


def get_cot_verdict(pair_name):
    """
    Возвращает COT-вердикт для пары.
    pair_name: 'XAU/USD', 'EUR/USD', 'GBP/USD'
    """
    if not COT_AVAILABLE:
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "COT недоступен"}

    # Маппинг названий пар на ключи cot_fetcher
    mapping = {
        "XAU/USD": "XAU (Золото)",
        "EUR/USD": "EUR/USD",
        "GBP/USD": "GBP/USD",
    }

    cot_key = mapping.get(pair_name)
    if not cot_key:
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Нет данных"}

    try:
        data = cot_fetcher.fetch_latest_data(cot_key, limit=2)
        if not data or len(data) < 2:
            return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Нет данных"}

        analysis = cot_fetcher.advanced_analysis(cot_key, data)
        if not analysis:
            return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Ошибка анализа"}

        verdict = analysis.get("verdict", {})
        return {
            "signal": verdict.get("signal", "neutral"),
            "score": verdict.get("score", 0),
            "direction": analysis.get("sentiment", {}).get("direction", "neutral"),
            "text": verdict.get("text", "N/A"),
        }
    except Exception as e:
        print(f"COT error for {pair_name}: {e}")
        return {"signal": "N/A", "score": 0, "direction": "neutral", "text": "Ошибка"}


def scan_all():
    """Главная функция — сканирует все пары и отправляет сигналы."""
    print(f"\n[{datetime.now()}] === SCAN START ===")

    for pair_name, oanda_instrument in config.PAIRS.items():
        try:
            print(f"\n--- {pair_name} ---")

            # 1. Получаем свечи
            df_d1 = client.get_candles(oanda_instrument, "D", count=20)
            df_h4 = client.get_candles(oanda_instrument, "H4", count=50)

            # 2. FVG детектор
            fvg_result = check_fvg_signals(df_d1, df_h4)
            print(f"  FVG: D1={fvg_result['d1_active']} H4={fvg_result['h4_active']} dir={fvg_result['direction']}")

            if not fvg_result["direction"]:
                print(f"  -> No FVG direction, skipping")
                continue

            # 3. COT вердикт
            cot = get_cot_verdict(pair_name)
            print(f"  COT: {cot['signal']} ({cot['text']})")

            # 4. Сигнальный движок
            signal = evaluate_setup(fvg_result, cot)

            if not signal["trade"]:
                print(f"  -> No trade: {signal['reason']}")
                continue

            print(f"  -> SIGNAL: {signal['direction']} risk={signal['risk_pct']}%")

            # 5. Фрактальный SL на H4
            price_data = client.get_current_price(oanda_instrument)
            entry_price = price_data["bid"] if signal["direction"] == "SELL" else price_data["ask"]

            fractal_tf = client.get_candles(oanda_instrument, "H4", count=50)
            sl_level = nearest_fractal(
                fractal_tf, fvg_result["direction"], entry_price
            )

            if sl_level is None:
                print(f"  -> No fractal SL found, skipping")
                continue

            print(f"  SL: {sl_level} (fractal)")

            # 6. Расчёт позиции
            account = client.get_account_summary()
            balance = float(account.get("balance", 0))

            pos = calculate_position(
                balance, entry_price, sl_level, signal["risk_pct"], pair_name
            )

            if pos.get("error"):
                print(f"  -> Risk error: {pos['error']}")
                continue

            print(f"  TP: {pos['tp_price']} | Units: {pos['units']} | Risk: ${pos['risk_amount']}")

            # 7. Отправка сигнала в Telegram
            signal_data = {
                **signal,
                "pair": pair_name,
                "entry_price": entry_price,
                "sl_price": pos["sl_price"],
                "tp_price": pos["tp_price"],
                "cot_text": cot.get("text", "N/A"),
                "risk_pct": signal["risk_pct"],
                "units": pos["units"],
                "risk_amount": pos["risk_amount"],
            }

            msg_id = send_signal(signal_data)
            if msg_id:
                print(f"  -> Signal sent to Telegram (msg #{msg_id})")

        except Exception as e:
            print(f"  [ERROR] {pair_name}: {e}")
            import traceback
            traceback.print_exc()

    print(f"[{datetime.now()}] === SCAN END ===\n")


# --- Startup ---

if __name__ == "__main__":
    print("=" * 60)
    print("COT + FVG Trading Bot")
    print(f"OANDA: {config.OANDA_ENV}")
    print(f"Pairs: {list(config.PAIRS.keys())}")
    print(f"Scan interval: {config.SCAN_INTERVAL_MINUTES} min")
    print(f"Telegram: {'configured' if config.TELEGRAM_TOKEN else 'MISSING'}")
    print("=" * 60)

    # Первый запуск сразу
    print("\n[Initial scan]")
    scan_all()

    # Планировщик
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        scan_all,
        trigger="interval",
        minutes=config.SCAN_INTERVAL_MINUTES,
        next_run_time=None,  # первое сканирование уже сделано
    )
    scheduler.start()
    print(f"\n[OK] Scheduler started (every {config.SCAN_INTERVAL_MINUTES} min)")

    # Держим процесс
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("\n[OK] Bot stopped")
