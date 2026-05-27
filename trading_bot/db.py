"""SQLite storage — сигналы, ордера, не теряются при перезапуске."""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "bot_history.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT, pair TEXT, direction TEXT,
            d1_fvg INTEGER, h4_fvg INTEGER, cot_text TEXT,
            entry_price REAL, sl_price REAL, tp_price REAL,
            volume REAL, risk_pct REAL, reason TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER, time TEXT,
            pair TEXT, direction TEXT,
            entry_price REAL, sl_price REAL, tp_price REAL,
            volume REAL, order_id INTEGER,
            FOREIGN KEY (signal_id) REFERENCES signals(id)
        )
    """)
    db.commit()
    db.close()


def save_signal(data):
    db = get_db()
    db.execute("""
        INSERT INTO signals (time, pair, direction, d1_fvg, h4_fvg, cot_text,
                             entry_price, sl_price, tp_price, volume, risk_pct, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(datetime.now()), data.get("pair"), data.get("direction"),
        int(data.get("d1_fvg", False)), int(data.get("h4_fvg", False)),
        data.get("cot_text", ""), data.get("entry_price"), data.get("sl_price"),
        data.get("tp_price"), data.get("volume"), data.get("risk_pct"),
        data.get("reason", "")
    ))
    sid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.commit()
    db.close()
    return sid


def save_order(signal_id, data, order_id):
    db = get_db()
    db.execute("""
        INSERT INTO orders (signal_id, time, pair, direction, entry_price, sl_price, tp_price, volume, order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (signal_id, str(datetime.now()), data.get("pair"), data.get("direction"),
          data.get("entry_price"), data.get("sl_price"), data.get("tp_price"),
          data.get("volume"), order_id))
    db.execute("UPDATE signals SET status='executed' WHERE id=?", (signal_id,))
    db.commit()
    db.close()


def get_order_history(limit=50):
    db = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_signal_history(limit=50):
    db = get_db()
    rows = db.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    db.close()
    return [dict(r) for r in rows]
