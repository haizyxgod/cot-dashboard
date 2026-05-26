from flask import Flask, render_template, jsonify
from flask_cors import CORS
from cot_fetcher import COTDataFetcher
import json
import os
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'cot_data.json')
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')

app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = os.environ.get('SECRET_KEY', 'cot-dashboard-dev-key')
CORS(app)

# Глобальный кэш данных
cot_data = {}
scheduler = None


def load_data():
    global cot_data
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            cot_data = json.load(f)
        print(f"[{datetime.now()}] Данные загружены из кэша")
    else:
        update_data()


def update_data():
    global cot_data
    print(f"[{datetime.now()}] Обновление данных COT...")
    fetcher = COTDataFetcher()
    cot_data = fetcher.save_data(DATA_FILE)
    print(f"[{datetime.now()}] Данные обновлены ({len(cot_data)-1} инструментов)")


def init_scheduler():
    global scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func=update_data,
        trigger='cron',
        day_of_week='sun',
        hour=13,  # 13:00 UTC = ~16:00 МСК / после публикации CFTC
        minute=0
    )
    scheduler.start()
    print(f"[{datetime.now()}] Планировщик запущен (воскресенье 13:00 UTC)")


# --- Инициализация при импорте (gunicorn) ---
load_data()
init_scheduler()


# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/instrument/<name>')
def instrument_detail(name):
    return render_template('instrument.html', instrument=name)


@app.route('/api/data')
def get_data():
    return jsonify(cot_data)


@app.route('/api/instrument/<name>')
def get_instrument_data(name):
    instrument_map = {
        'gold': 'XAU (Золото)',
        'silver': 'XAG (Серебро)',
        'eur': 'EUR/USD',
        'gbp': 'GBP/USD',
        'usdjpy': 'USD/JPY',
        'aud': 'AUD/USD'
    }
    instrument_name = instrument_map.get(name)
    if instrument_name and instrument_name in cot_data:
        return jsonify({
            'instrument': instrument_name,
            'data': cot_data[instrument_name],
            'metadata': cot_data.get('metadata')
        })
    return jsonify({'error': 'Instrument not found'}), 404


@app.route('/api/update')
def force_update():
    update_data()
    return jsonify({"status": "success", "message": "Данные обновлены"})


@app.route('/health')
def health():
    return jsonify({
        "status": "ok",
        "instruments": len(cot_data) - 1 if 'metadata' in cot_data else 0,
        "last_updated": cot_data.get('metadata', {}).get('last_updated')
    })


# --- Dev-сервер ---

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    print("=" * 60)
    print("COT Dashboard (dev mode)")
    print(f"http://localhost:{port}")
    print("=" * 60)
    app.run(debug=debug, host='0.0.0.0', port=port, use_reloader=False)
