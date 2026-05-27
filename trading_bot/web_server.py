"""Web UI — компактные сигналы, позиции, история."""
import threading
from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS
import db as database

app = Flask(__name__)
CORS(app)

pending_signals = {}

# Init DB
database.init()

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>FIN Trading Bot</title>
    <style>
        * { box-sizing:border-box; margin:0; padding:0; }
        body { background:#0d0d0d; color:#ccc; font-family:system-ui,sans-serif; padding:16px; }
        .container { max-width:700px; margin:0 auto; }
        .header { display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; padding-bottom:10px; border-bottom:1px solid #222; }
        .header h1 { font-size:1.2rem; color:#fff; }
        .dot { width:7px; height:7px; border-radius:50%; display:inline-block; margin-right:5px; background:#00e676; animation:pulse 2s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.2} }
        .nav { display:flex; gap:14px; margin-bottom:14px; }
        .nav a { color:#666; text-decoration:none; font-size:0.85rem; font-weight:600; }
        .nav a:hover,.nav a.active { color:#fff; }
        .msg { padding:8px 14px; border-radius:6px; margin:10px 0; font-size:0.85rem; }
        .msg-ok { background:#0d3320; color:#00e676; border:1px solid #1a4d32; }
        .msg-err { background:#330d0d; color:#ff5252; border:1px solid #4d1a1a; }

        /* Signal row */
        .sig { background:#141414; border-radius:8px; padding:10px 14px; margin:8px 0; display:flex; align-items:center; gap:12px; border-left:3px solid #333; }
        .sig.buy { border-left-color:#00e676; }
        .sig.sell { border-left-color:#ff5252; }
        .sig .dir { font-weight:700; font-size:0.9rem; min-width:50px; }
        .sig .dir.buy-c { color:#00e676; }
        .sig .dir.sell-c { color:#ff5252; }
        .sig .info { flex:1; font-size:0.75rem; color:#888; }
        .sig .info span { color:#aaa; font-weight:600; }
        .sig .prices { font-size:0.8rem; text-align:right; min-width:90px; }
        .sig .prices .sl { color:#ff5252; }
        .sig .prices .tp { color:#00e676; }
        .sig .act { display:flex; gap:4px; }
        .sig .act button { padding:5px 10px; border:none; border-radius:4px; font-size:0.7rem; font-weight:700; cursor:pointer; color:#fff; }
        .btn-ok { background:#00c853; }
        .btn-no { background:transparent; border:1px solid #ff5252!important; color:#ff5252!important; }

        /* Positions */
        .pos { background:#141414; border-radius:8px; padding:10px 14px; margin:6px 0; display:flex; align-items:center; gap:12px; }
        .pos .pnl-pos { color:#00e676; }
        .pos .pnl-neg { color:#ff5252; }

        /* History */
        .hist { background:#141414; border-radius:8px; padding:10px 14px; margin:6px 0; font-size:0.8rem; }
        .hist .tag { font-size:0.65rem; padding:2px 6px; border-radius:3px; }
        .tag-exec { background:#00c853; color:#000; }
        .tag-rej { background:#ff5252; color:#fff; }

        .empty { text-align:center; padding:40px; color:#555; }
    </style>
</head>
<body>
<div class="container">

<div class="header">
    <div>
        <h1>FIN Trading Bot</h1>
        <span style="font-size:0.7rem;color:#666">След. сканирование: <b id="timer">--:--</b></span>
    </div>
    <span style="font-size:0.75rem;color:#888"><span class="dot"></span> MT5</span>
</div>

<div class="nav">
    <a href="/bot" class="{{ 'active' if tab == 'signals' else '' }}">Сигналы</a>
    <a href="/bot/positions" class="{{ 'active' if tab == 'positions' else '' }}">Позиции</a>
    <a href="/bot/history" class="{{ 'active' if tab == 'history' else '' }}">История</a>
</div>

{% if msg %}<div class="msg {{ msg_type }}">{{ msg }}</div>{% endif %}

{% if tab == 'signals' %}
    {% if signals %}
        {% for s in signals %}
        <div class="sig {{ 'buy' if s.direction == 'BUY' else 'sell' }}">
            <div class="dir {{ 'buy-c' if s.direction == 'BUY' else 'sell-c' }}">
                {{ '▲' if s.direction == 'BUY' else '▼' }} {{ s.pair }}
            </div>
            <div class="info">
                D1:<span>{{ '✅' if s.d1_fvg else '❌' }}</span>
                H4:<span>{{ '✅' if s.h4_fvg else '❌' }}</span>
                COT:<span>{{ s.cot_text }}</span>
                Lot:<span>{{ s.volume }}</span>
                Risk:<span style="color:#ff9800">{{ s.risk_pct }}%</span>
            </div>
            <div class="prices">
                <div>{{ s.entry_price }}</div>
                <div class="sl">SL {{ s.sl_price }}</div>
                <div class="tp">TP {{ s.tp_price }}</div>
            </div>
            <div class="act">
                <form method="POST" action="/bot/accept/{{ s.id }}"><button class="btn-ok">✓</button></form>
                <form method="POST" action="/bot/reject/{{ s.id }}"><button class="btn-no">✕</button></form>
            </div>
        </div>
        {% endfor %}
    {% else %}
        <div class="empty"><p>Нет сигналов</p><p style="font-size:0.75rem">Жду сканирования...</p></div>
    {% endif %}

{% elif tab == 'positions' %}
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <span style="font-size:0.85rem;color:#888">{{ count }} поз. | P&L: <b class="{{ 'pnl-pos' if total_pnl >= 0 else 'pnl-neg' }}">{{ total_pnl }}</b></span>
        {% if positions %}
        <form method="POST" action="/bot/close_all">
            <button style="padding:6px 14px;background:#ff5252;color:#fff;border:none;border-radius:4px;font-weight:700;cursor:pointer;font-size:0.8rem">Закрыть всё</button>
        </form>
        {% endif %}
    </div>
    {% if positions %}
        {% for p in positions %}
        <div class="pos">
            <div style="font-weight:700;min-width:70px">{{ p.symbol }}</div>
            <div style="font-size:0.8rem;color:#888">{{ '▲ BUY' if p.type == 0 else '▼ SELL' }}</div>
            <div style="flex:1;font-size:0.8rem">
                Vol: {{ p.volume }} | Open: {{ p.price_open }} | Curr: {{ p.price_current }}
            </div>
            <div style="font-weight:700;min-width:80px;text-align:right" class="{{ 'pnl-pos' if p.profit > 0 else 'pnl-neg' }}">
                {{ p.profit }}
            </div>
        </div>
        {% endfor %}
    {% else %}
        <div class="empty"><p>Нет открытых позиций</p></div>
    {% endif %}

{% elif tab == 'history' %}
    {% if history %}
        {% for h in history %}
        <div class="hist">
            <span class="tag tag-exec">#{{ h.order_id }}</span>
            {{ h.pair }} {{ h.direction }}
            <span style="color:#888;margin-left:8px">@{{ h.entry_price }}</span>
            <span style="color:#888;margin-left:8px;font-size:0.7rem">{{ h.time[:19] }}</span>
        </div>
        {% endfor %}
    {% else %}
        <div class="empty"><p>История пуста</p></div>
    {% endif %}
{% endif %}

</div>

<script>
// Countdown to next scan
var scanMin = {{ scan_interval }};
function updateTimer() {
    var now = new Date();
    var next = new Date(now);
    next.setMinutes(Math.ceil(now.getMinutes() / scanMin) * scanMin, 0, 0);
    var diff = Math.floor((next - now) / 1000);
    var h = Math.floor(diff / 3600);
    var m = Math.floor((diff % 3600) / 60);
    document.getElementById('timer').textContent = h + 'h ' + m + 'm';
}
setInterval(updateTimer, 1000);
updateTimer();
</script>
</body>
</html>"""


@app.route("/bot")
def bot_page():
    signals = [{**s, "id": sid} for sid, s in pending_signals.items()]
    return render_template_string(HTML, signals=signals, tab="signals", msg=None, msg_type="", positions=[], history=[], scan_interval=180)


@app.route("/bot/positions")
def positions_page():
    try:
        from mt5_client import client as mt5
        mt5.connect()
        summary = mt5.get_positions_summary()
        mt5.disconnect()
        positions = summary["positions"]
        total_pnl = summary["total_pnl"]
        count = summary["count"]
    except Exception:
        positions = []; total_pnl = 0; count = 0
    return render_template_string(HTML, positions=positions, total_pnl=total_pnl,
                                  count=count, tab="positions", msg=None, msg_type="",
                                  signals=[], history=[])


@app.route("/bot/close_all", methods=["POST"])
def close_all():
    try:
        from mt5_client import client as mt5
        mt5.connect()
        n = mt5.close_all_positions()
        mt5.disconnect()
        msg = f"Закрыто позиций: {n}"
        msg_type = "msg-ok"
    except Exception as e:
        msg = f"Ошибка: {e}"
        msg_type = "msg-err"
    # Re-render positions
    try:
        from mt5_client import client as mt5
        mt5.connect()
        summary = mt5.get_positions_summary()
        mt5.disconnect()
    except Exception:
        summary = {"positions": [], "total_pnl": 0, "count": 0}
    return render_template_string(HTML, positions=summary["positions"],
                                  total_pnl=summary["total_pnl"], count=summary["count"],
                                  tab="positions", msg=msg, msg_type=msg_type,
                                  signals=[], history=[])


@app.route("/bot/history")
def history_page():
    hist = database.get_order_history(50)
    return render_template_string(HTML, history=hist, tab="history", msg=None, msg_type="", signals=[], positions=[], scan_interval=180)


@app.route("/bot/accept/<sig_id>", methods=["POST"])
def accept(sig_id):
    sig_id = int(sig_id)
    sig = pending_signals.pop(sig_id, None)
    if sig is None:
        return render_template_string(HTML, signals=[], tab="signals", msg="Сигнал устарел", msg_type="msg-err", positions=[], history=[])

    try:
        import config
        from mt5_client import client as mt5
        from fractal_detector import nearest_fractal, calculate_atr
        from risk_manager import calculate_lot

        mt5.connect()
        symbol = config.PAIRS.get(sig["pair"])
        tick = mt5.get_current_price(symbol)
        entry = tick["bid"] if sig["direction"] == "SELL" else tick["ask"]
        fvg_dir = "bearish" if sig["direction"] == "SELL" else "bullish"
        df_h4 = mt5.get_candles(symbol, "H4", 50)
        atr_val = calculate_atr(df_h4, 14)
        sl = nearest_fractal(df_h4, fvg_dir, entry, atr_value=atr_val)

        if sl is None:
            mt5.disconnect()
            return "<script>alert('Не найден фрактал'); window.location='/bot'</script>"

        acc = mt5.get_account_summary()
        info = mt5.get_symbol_info(symbol)
        balance = float(acc.get("balance", 0))
        pos = calculate_lot(balance, entry, sl, sig["risk_pct"], sig["pair"],
                            info["point"], info["trade_contract_size"],
                            info["trade_tick_value"])
        tp = pos["tp_price"]

        result = mt5.place_market_order(symbol, sig["direction"], float(sl),
                                         float(tp), float(pos["volume"]))

        if result is None:
            mt5.disconnect()
            err = (f"ORDER FAILED: {sig['pair']} {sig['direction']} "
                   f"Entry={entry:.5f} SL={sl:.5f} TP={tp:.5f} Lot={pos['volume']}")
            print(err)
            return f"<script>alert('{err}'); window.location='/bot'</script>"

        # Register for BE tracking
        import main as _main
        _main.register_be(result["order"], symbol, entry, sig["direction"])

        mt5.disconnect()

        sig["status"] = "executed"
        sig["order"] = result.get("order")
        sig["entry_price"] = entry
        sig["sl_price"] = sl
        sig["tp_price"] = tp

        # Save to DB
        sid = database.save_signal(sig)
        if sig.get("order"):
            database.save_order(sid, sig, sig["order"])

        order_id = sig.get("order", "?")
        msg = f"#{order_id} — {sig['pair']} {sig['direction']} Entry={entry:.5f}"
        return f"<script>alert('{msg}'); window.location='/bot'</script>"
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"<script>alert('Ошибка: {e}'); window.location='/bot'</script>"


@app.route("/bot/reject/<sig_id>", methods=["POST"])
def reject(sig_id):
    sig_id = int(sig_id)
    sig = pending_signals.pop(sig_id, None)
    if sig:
        sig["status"] = "rejected"
        database.save_signal(sig)
    return "<script>window.location='/bot'</script>"


def run_web(port=5002):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def start_web():
    t = threading.Thread(target=run_web, daemon=True)
    t.start()
    print(f"[Web] UI at http://localhost:5002/bot")
    return t
