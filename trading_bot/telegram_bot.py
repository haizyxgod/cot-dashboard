"""Telegram Bot — отправка сигналов и приём подтверждений."""
import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
import config

bot = Bot(token=config.TELEGRAM_TOKEN)
chat_id = config.TELEGRAM_CHAT_ID


def send_signal(signal_data):
    """
    Отправляет торговый сигнал в Telegram с кнопками [Принять] [Отклонить].

    signal_data: dict из signal_engine.evaluate_setup() + extra fields
    """
    pair = signal_data.get("pair", "???")
    direction = signal_data.get("direction", "???")
    emoji = "🟢" if direction == "BUY" else "🔴" if direction == "SELL" else "⚪"

    d1_ok = "✅" if signal_data.get("d1_fvg") else "❌"
    h4_ok = "✅" if signal_data.get("h4_fvg") else "❌"
    cot_text = signal_data.get("cot_text", "N/A")
    risk = signal_data.get("risk_pct", 0)
    entry = signal_data.get("entry_price", 0)
    sl = signal_data.get("sl_price", 0)
    tp = signal_data.get("tp_price", 0)
    reason = signal_data.get("reason", "")

    msg = (
        f"{emoji} <b>{pair} {direction}</b>\n"
        f" ┣ D1 FVG: {d1_ok}  |  H4 FVG: {h4_ok}  |  COT: {cot_text}\n"
        f" ┣ Вход: <code>{entry}</code>\n"
        f" ┣ SL: <code>{sl}</code>  |  TP: <code>{tp}</code>\n"
        f" ┣ Риск: <b>{risk}%</b>\n"
        f" ┗ <i>{reason}</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Принять", callback_data=f"accept|{pair}|{direction}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject|{pair}|{direction}"),
        ]
    ])

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        message = loop.run_until_complete(
            bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML",
                             reply_markup=keyboard)
        )
        loop.close()
        return message.message_id
    except TelegramError as e:
        print(f"Telegram error: {e}")
        return None


def send_text(text):
    """Отправляет простое сообщение."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        )
        loop.close()
    except TelegramError as e:
        print(f"Telegram error: {e}")


def notify_order_filled(pair, direction, entry, sl, tp):
    """Уведомление об исполнении ордера."""
    emoji = "🟢" if direction == "BUY" else "🔴"
    msg = (
        f"{emoji} <b>{pair} {direction} — ОРДЕР ИСПОЛНЕН</b>\n"
        f"Вход: <code>{entry}</code>\n"
        f"SL: <code>{sl}</code>  |  TP: <code>{tp}</code>"
    )
    send_text(msg)


def notify_closed(pair, direction, pnl, reason="TP/SL"):
    """Уведомление о закрытии позиции."""
    emoji = "✅" if pnl > 0 else "❌"
    msg = (
        f"{emoji} <b>{pair} {direction} — ЗАКРЫТО ({reason})</b>\n"
        f"P&L: <b>${pnl:+.2f}</b>"
    )
    send_text(msg)
