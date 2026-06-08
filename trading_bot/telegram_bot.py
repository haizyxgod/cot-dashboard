"""Telegram Bot — full control with persistent keyboard, polling, and inline actions."""
import time
import requests
import threading
from datetime import datetime

import config

TELEGRAM_API = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"
CHAT_ID = config.TELEGRAM_CHAT_ID

pending_signals = {}
order_callback = None
last_update_id = 0

# ── Persistent reply keyboard ──────────────────────────────────────────
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Статус"}, {"text": "📋 Позиции"}, {"text": "📈 Сегодня"}],
        [{"text": "🔍 Скан"},   {"text": "🔄 Режим"},  {"text": "📜 История"}],
        [{"text": "🛑 Закрыть всё"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def _api(method, data=None):
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=data or {}, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[TG] API error: {e}")
        return None


def set_order_callback(fn):
    global order_callback
    order_callback = fn


# ── One-way sends (used by main.py) ────────────────────────────────────

def send_text(text):
    _api("sendMessage", {
        "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
        "reply_markup": MAIN_KEYBOARD,
    })


def send_signal(signal_data):
    """Old interactive signal with Accept/Reject buttons (kept for backwards compat)."""
    direction = signal_data.get("direction", "???")
    emoji = "🟢" if direction == "BUY" else "🔴"
    msg = (
        f"{emoji} *{signal_data.get('pair')} {direction}*\n"
        f" ┣ D1 FVG: {'✅' if signal_data.get('d1_fvg') else '❌'}  "
        f"|  H4 FVG: {'✅' if signal_data.get('h4_fvg') else '❌'}  "
        f"|  COT: {signal_data.get('cot_text', 'N/A')}\n"
        f" ┣ Вход: `{signal_data.get('entry_price')}`\n"
        f" ┣ SL: `{signal_data.get('sl_price')}`  "
        f"|  TP: `{signal_data.get('tp_price')}`\n"
        f" ┣ Лот: *{signal_data.get('volume')}*  "
        f"|  Риск: *{signal_data.get('risk_pct')}%*\n"
        f" ┗ _{signal_data.get('reason')}_"
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Принять", "callback_data": "accept"},
            {"text": "❌ Отклонить", "callback_data": "reject"},
        ]]
    }
    result = _api("sendMessage", {
        "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown",
        "reply_markup": keyboard,
    })
    if result and result.get("ok"):
        pending_signals[result["result"]["message_id"]] = signal_data
        return result["result"]["message_id"]
    return None


# ── Helper: send with keyboard ─────────────────────────────────────────

def _send(msg, keyboard=None, parse="Markdown"):
    """Send message; if keyboard is None, attaches MAIN_KEYBOARD."""
    data = {"chat_id": CHAT_ID, "text": msg, "parse_mode": parse}
    if keyboard is not None:
        data["reply_markup"] = keyboard
    else:
        data["reply_markup"] = MAIN_KEYBOARD
    return _api("sendMessage", data)


def _edit(msg_id, msg, keyboard=None, parse="Markdown"):
    data = {"chat_id": CHAT_ID, "message_id": msg_id, "text": msg, "parse_mode": parse}
    if keyboard:
        data["reply_markup"] = keyboard
    return _api("editMessageText", data)


def _answer_cb(cb_id):
    _api("answerCallbackQuery", {"callback_query_id": cb_id})


# ── Command handlers ───────────────────────────────────────────────────

def cmd_status():
    """📊 Full status: balance, equity, positions, scan, MT5."""
    try:
        from mt5_client import client as mt5
        import web_server
    except Exception as e:
        return f"❌ Ошибка импорта: {e}"

    try:
        mt5.connect()
        acc = mt5.get_account_summary()
        positions = mt5.get_positions()
        mt5.disconnect()
    except Exception as e:
        return f"❌ MT5 ошибка: {e}"

    balance = float(acc.get("balance", 0))
    equity = float(acc.get("equity", 0))
    open_pnl = equity - balance
    pos_count = len(positions)
    be_count = 0  # be_tracked lives in main.py process memory, skip for TG

    state = web_server.bot_state
    mt5_ok = "🟢" if state.get("mt5_connected") else "🔴"
    auto = "АВТО" if state.get("auto_mode", True) else "РУЧН"
    last_scan = state.get("last_scan", "—")
    if last_scan and last_scan != "—":
        last_scan = last_scan[:19].replace("T", " ")

    pnl_emoji = "📈" if open_pnl >= 0 else "📉"
    return (
        f"*📊 Статус*\n"
        f" ┣ MT5: {mt5_ok} | Режим: *{auto}*\n"
        f" ┣ Баланс: `${balance:,.2f}`\n"
        f" ┣ Equity: `${equity:,.2f}`\n"
        f" ┣ {pnl_emoji} Открытый P&L: `{open_pnl:+,.2f}`\n"
        f" ┣ Позиций: *{pos_count}* | В BE: *{be_count}*\n"
        f" ┣ Последний скан: `{last_scan}`\n"
        f" ┗ Пары: {', '.join(config.PAIRS.keys())}"
    )


def cmd_positions():
    """📋 List open positions with inline [BE] [Close] buttons."""
    try:
        from mt5_client import client as mt5
    except Exception as e:
        return None, f"❌ Ошибка: {e}"

    try:
        mt5.connect()
        positions = mt5.get_positions()
        mt5.disconnect()
    except Exception as e:
        return None, f"❌ MT5 ошибка: {e}"

    if not positions:
        return None, "📋 Нет открытых позиций."

    # Get BE state from main module
    try:
        import sys
        _main = sys.modules.get("__main__", sys.modules.get("main"))
        be_tracked = getattr(_main, "be_tracked", {})
    except Exception:
        be_tracked = {}

    keyboard = {"inline_keyboard": []}
    lines = ["*📋 Позиции*"]
    for p in positions:
        ticket = p["ticket"]
        symbol = p["symbol"]
        ptype = "🟢 BUY" if p["type"] == 0 else "🔴 SELL"
        entry = p.get("price_open", p.get("open_price", 0))
        sl = float(p.get("sl", 0))
        tp = float(p.get("tp", 0))
        profit = float(p.get("profit", 0))

        # Check if BE: from tracked state OR SL already at entry
        be_info = be_tracked.get(ticket, {})
        is_be = be_info.get("be_triggered", False)
        if not is_be and entry:
            is_be = abs(sl - entry) < abs(entry) * 0.0001

        be_tag = " 🟡BE" if is_be else ""
        pnl_sign = "+" if profit >= 0 else ""

        entry_str = f"`{entry}`"
        sl_str = f"`{sl}`"
        if is_be:
            sl_str = f"`{sl}` 🟡BE"

        lines.append(
            f" ┣ *{symbol}* {ptype} #{ticket}{be_tag}\n"
            f" ┃ Entry: {entry_str} | SL: {sl_str} | TP: `{tp}`\n"
            f" ┃ P&L: `{pnl_sign}{profit:.2f}`"
        )
        row = []
        if not is_be:
            row.append({"text": f"🟡 BE #{ticket}", "callback_data": f"be_{ticket}"})
        row.append({"text": f"❌ Закрыть #{ticket}", "callback_data": f"close_{ticket}"})
        keyboard["inline_keyboard"].append(row)
    lines[-1] = lines[-1].replace(" ┣", " ┗")

    return keyboard, "\n".join(lines)


def cmd_today():
    """📈 Today's P&L and stats."""
    try:
        import db as database
    except Exception as e:
        return f"❌ Ошибка: {e}"

    today = datetime.now().strftime("%Y-%m-%d")
    try:
        orders = database.get_order_history(500)
    except Exception as e:
        return f"❌ DB ошибка: {e}"

    today_orders = [o for o in orders if str(o.get("time", "")).startswith(today)]
    if not today_orders:
        return "📈 Сегодня сделок нет."

    total_pnl = sum(o.get("pnl", 0) or 0 for o in today_orders)
    wins = sum(1 for o in today_orders if (o.get("pnl", 0) or 0) > 0)
    losses = sum(1 for o in today_orders if (o.get("pnl", 0) or 0) < 0)
    total = len(today_orders)
    wr = f"{wins / total * 100:.0f}%" if total else "—"

    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    return (
        f"*📈 Сегодня ({today})*\n"
        f" ┣ Сделок: *{total}* (✅{wins} / ❌{losses})\n"
        f" ┣ Win-rate: *{wr}*\n"
        f" ┗ {pnl_emoji} P&L: `{total_pnl:+,.2f}`"
    )


def cmd_scan():
    """🔍 Trigger manual scan."""
    try:
        import sys; _main = sys.modules.get("__main__", sys.modules.get("main"))
        _main.scan_all(is_manual=True)
        return "🔍 Скан выполнен — проверьте дашборд."
    except Exception as e:
        return f"❌ Ошибка сканирования: {e}"


def cmd_mode():
    """🔄 Toggle auto/manual mode."""
    try:
        import web_server
    except Exception as e:
        return f"❌ Ошибка: {e}"
    web_server.bot_state["auto_mode"] = not web_server.bot_state.get("auto_mode", True)
    mode = "АВТО" if web_server.bot_state["auto_mode"] else "РУЧНОЙ"
    web_server.add_log(f"TG: режим → {mode}")
    return f"🔄 Режим переключён: *{mode}*"


def cmd_history():
    """📜 Last 5 closed trades."""
    try:
        import db as database
    except Exception as e:
        return f"❌ Ошибка: {e}"

    try:
        orders = database.get_order_history(50)
    except Exception as e:
        return f"❌ DB ошибка: {e}"

    closed = [o for o in orders if o.get("result") in ("win", "loss", "be")]
    if not closed:
        return "📜 История пуста."

    lines = ["*📜 Последние сделки*"]
    for o in closed[:5]:
        pnl = o.get("pnl", 0) or 0
        result = o.get("result", "?")
        emoji = {"win": "✅", "loss": "❌", "be": "➖"}.get(result, "❓")
        pair = o.get("pair", "?")
        direction = o.get("direction", "?")
        time_str = str(o.get("time", ""))[:19]
        lines.append(f"{emoji} *{pair}* {direction} | P&L: `{pnl:+,.2f}` | {time_str}")

    return "\n".join(lines)


def cmd_close_all():
    """🛑 Request confirmation before closing all positions."""
    try:
        from mt5_client import client as mt5
        mt5.connect()
        positions = mt5.get_positions()
        mt5.disconnect()
    except Exception as e:
        return None, f"❌ MT5 ошибка: {e}", None

    if not positions:
        return None, "🛑 Нет открытых позиций.", None

    total_pnl = sum(float(p.get("profit", 0)) for p in positions)
    lines = [
        f"*🛑 Закрыть все позиции?*",
        f"Позиций: *{len(positions)}* | P&L: `{total_pnl:+,.2f}`",
        f"Это действие нельзя отменить.",
    ]
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Да, закрыть всё", "callback_data": "closeall_yes"},
        {"text": "❌ Отмена", "callback_data": "closeall_no"},
    ]]}
    return keyboard, "\n".join(lines), None


def cmd_start():
    return (
        "🤖 *FIN Trading Bot*\n\n"
        "Используйте кнопки внизу или команды:\n"
        "/status, /positions, /today, /scan, /mode, /history, /close_all"
    )


# ── Inline callback handlers ───────────────────────────────────────────

def _handle_be(ticket_str):
    ticket = int(ticket_str)
    try:
        from mt5_client import client as mt5
        mt5.connect()
        pos_list = mt5.get_positions()
        pos = next((p for p in pos_list if p["ticket"] == ticket), None)
        if not pos:
            mt5.disconnect()
            return f"❌ Позиция #{ticket} не найдена."
        entry = pos.get("price_open", pos.get("open_price", 0))
        ok = mt5.modify_sl(ticket, entry)
        mt5.disconnect()
        if ok:
            return f"🟡 #{ticket}: SL → BE (`{entry}`)"
        return f"❌ Не удалось изменить SL для #{ticket}"
    except Exception as e:
        return f"❌ Ошибка BE #{ticket}: {e}"


def _handle_close(ticket_str):
    ticket = int(ticket_str)
    try:
        from mt5_client import client as mt5
        mt5.connect()
        ok = mt5.close_position(ticket)
        mt5.disconnect()
        if ok:
            return f"❌ Позиция #{ticket} закрыта."
        return f"❌ Не удалось закрыть #{ticket}"
    except Exception as e:
        return f"❌ Ошибка закрытия #{ticket}: {e}"


def _handle_closeall_confirm():
    try:
        from mt5_client import client as mt5
        mt5.connect()
        count = mt5.close_all_positions()
        mt5.disconnect()
        return f"🛑 Закрыто позиций: *{count}*"
    except Exception as e:
        return f"❌ Ошибка: {e}"


# ── Message router ─────────────────────────────────────────────────────

ROUTES = {
    "/start": cmd_start,
    "📊 Статус": cmd_status, "/status": cmd_status,
    "📋 Позиции": cmd_positions, "/positions": cmd_positions,
    "📈 Сегодня": cmd_today, "/today": cmd_today,
    "🔍 Скан": cmd_scan, "/scan": cmd_scan,
    "🔄 Режим": cmd_mode, "/mode": cmd_mode,
    "📜 История": cmd_history, "/history": cmd_history,
    "🛑 Закрыть всё": cmd_close_all, "/close_all": cmd_close_all,
}


def handle_message(text):
    """Route an incoming text message to the right handler. Returns text to send."""
    handler = ROUTES.get(text)
    if handler is None:
        return "Используйте кнопки внизу или /help"

    result = handler()

    # Some handlers return (keyboard, text), others return just text
    if isinstance(result, tuple):
        if len(result) == 3:  # cmd_close_all
            keyboard, resp_text = result[0], result[1]
            return resp_text, keyboard
        keyboard, resp_text = result
        return resp_text, keyboard
    return result, MAIN_KEYBOARD


# ── Polling ─────────────────────────────────────────────────────────────

def handle_callback(callback):
    """Handle inline button presses (BE, Close, close_all confirm, old accept/reject)."""
    msg = callback.get("message", {})
    msg_id = msg.get("message_id")
    data = callback.get("data", "")
    cb_id = callback["id"]

    # Old accept/reject flow
    if data in ("accept", "reject"):
        _answer_cb(cb_id)
        sig = pending_signals.pop(msg_id, None)
        if sig is None:
            _edit(msg_id, (msg.get("text", "") or "?") + "\n\n⏳ Сигнал устарел")
            return
        pair = sig.get("pair", "???")
        direction = sig.get("direction", "???")
        if data == "accept":
            _edit(msg_id, (msg.get("text", "") or "?") + "\n\n✅ *ПРИНЯТО*", parse="Markdown")
            if order_callback:
                result = order_callback(pair, direction, sig)
                if result:
                    send_text(f"✅ *{pair} {direction}* — ордер #{result.get('order', '?')}")
                else:
                    send_text(f"❌ Ошибка выставления {pair}")
        else:
            _edit(msg_id, (msg.get("text", "") or "?") + "\n\n❌ Отклонено")
        return

    # New action callbacks
    if data.startswith("be_"):
        _answer_cb(cb_id)
        ticket = data[3:]
        resp = _handle_be(ticket)
        _edit(msg_id, (msg.get("text", "") or "?") + f"\n\n{resp}")
        return

    if data.startswith("close_"):
        _answer_cb(cb_id)
        ticket = data[6:]
        resp = _handle_close(ticket)
        _edit(msg_id, (msg.get("text", "") or "?") + f"\n\n{resp}")
        return

    if data == "closeall_yes":
        _answer_cb(cb_id)
        resp = _handle_closeall_confirm()
        _edit(msg_id, resp)
        return

    if data == "closeall_no":
        _answer_cb(cb_id)
        _edit(msg_id, (msg.get("text", "") or "?") + "\n\n❌ Отменено")
        return

    # Unknown callback
    _answer_cb(cb_id)


def poll_once():
    global last_update_id
    result = _api("getUpdates", {
        "offset": last_update_id + 1,
        "timeout": 10,
        "allowed_updates": ["callback_query", "message"],
    })
    if not result or not result.get("ok"):
        return

    for update in result["result"]:
        last_update_id = max(last_update_id, update["update_id"])

        if "callback_query" in update:
            cb = update["callback_query"]
            sender_id = str(cb.get("message", {}).get("chat", {}).get("id", ""))
            if sender_id == str(CHAT_ID):
                handle_callback(cb)

        elif "message" in update:
            msg = update["message"]
            # Verify sender is authorized
            sender_id = str(msg.get("chat", {}).get("id", ""))
            if sender_id != str(CHAT_ID):
                continue  # silently ignore unknown senders
            text = msg.get("text", "")
            if not text:
                continue

            resp = handle_message(text)
            if isinstance(resp, tuple):
                resp_text, kb = resp
                if kb is None:
                    kb = MAIN_KEYBOARD
                _send(resp_text, keyboard=kb)
            else:
                _send(resp)


def start_polling():
    """Entry point for daemon thread — infinite polling loop."""
    print("[TG] Polling started (full control mode)")
    while True:
        try:
            poll_once()
            time.sleep(2)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[TG] Poll error: {e}")
            time.sleep(5)


def run_bot():
    """Legacy entry point (kept for backwards compat)."""
    start_polling()
