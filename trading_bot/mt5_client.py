"""MT5 Client — свечи, цены, ордера через MetaTrader5 Python API."""
import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime
import config


class MT5Client:
    """MT5 API wrapper with auto-reconnect."""

    def __init__(self):
        self.connected = False
        self._reconnect_attempts = 0
        self._max_retries = 10
        self._base_delay = 30  # seconds
        import threading
        self._lock = threading.Lock()

    def connect(self):
        if self.connected:
            # Verify connection is still alive
            try:
                info = mt5.account_info()
                if info is None:
                    print("[MT5] Connection lost — reconnecting...")
                    self.connected = False
                    mt5.shutdown()
                else:
                    return True
            except Exception:
                self.connected = False

        self._reconnect_attempts += 1

        ok = mt5.initialize(
            login=config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )
        if ok:
            self.connected = True
            self._reconnect_attempts = 0
            print(f"[MT5] Connected to {config.MT5_SERVER}")
            return True

        # Backoff: 30s, 60s, 120s, 240s... max 10 min
        delay = min(self._base_delay * (2 ** (self._reconnect_attempts - 1)), 600)
        err = mt5.last_error()
        print(f"[MT5] Connection failed (#{self._reconnect_attempts}): {err}. "
              f"Retry in {delay}s")
        if self._reconnect_attempts <= self._max_retries:
            import time
            time.sleep(delay)
            return self.connect()  # recursive retry
        return False

    def disconnect(self):
        with self._lock:
            mt5.shutdown()
            self.connected = False
            self._reconnect_attempts = 0

    # --- Candles ---

    def get_candles(self, symbol, timeframe, count=50):
        """symbol: 'XAUUSD', 'USDJPY'
           timeframe: 'D', 'H4', 'H1' → mt5.TIMEFRAME_D1, etc.
        """
        tf_map = {
            "D": mt5.TIMEFRAME_D1,
            "H4": mt5.TIMEFRAME_H4,
            "H1": mt5.TIMEFRAME_H1,
        }
        self.connect()
        rates = mt5.copy_rates_from_pos(symbol, tf_map[timeframe], 0, count)
        if rates is None or len(rates) == 0:
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df[["time", "open", "high", "low", "close", "volume"]]

    def get_current_price(self, symbol):
        """Возвращает ask/bid."""
        self.connect()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return {"bid": 0, "ask": 0}
        return {"bid": tick.bid, "ask": tick.ask}

    # --- Orders ---

    def place_market_order(self, symbol, direction, sl, tp, volume):
        """
        Исполняет рыночный ордер СЕЙЧАС.
        direction: 'BUY' or 'SELL'
        """
        self.connect()

        # Get current price
        tick = mt5.symbol_info_tick(symbol)
        if direction == "BUY":
            price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
        else:
            price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 50,
            "magic": 123456,
            "comment": "FIN Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"[MT5] Market order failed: {result.comment} (code {result.retcode})")
            return None
        print(f"[MT5] MARKET {direction} {symbol} @ {price} vol={volume} | #{result.order}")
        return {"order": result.order, "price": price, "volume": volume}

    def place_stop_order(self, symbol, direction, price, sl, tp, volume):
        self.connect()
        order_type = mt5.ORDER_TYPE_BUY_STOP if direction == "BUY" else mt5.ORDER_TYPE_SELL_STOP
        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": 123456,
            "comment": "COT+FVG Bot",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"[MT5] Order failed: {result.comment} (code {result.retcode})")
            return None
        print(f"[MT5] Order #{result.order} placed: {direction} {symbol} @ {price}")
        return result._asdict()

    def get_positions(self, symbol=None):
        """Открытые позиции."""
        with self._lock:
            self.connect()
            positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
            return [p._asdict() for p in positions] if positions else []

    def close_position(self, ticket):
        """Закрывает одну позицию по тикету."""
        self.connect()
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            return False
        pos = pos[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        close_type = mt5.ORDER_TYPE_BUY if pos.type == mt5.ORDER_TYPE_SELL else mt5.ORDER_TYPE_SELL
        price = tick.ask if pos.type == mt5.ORDER_TYPE_SELL else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "deviation": 50,
            "magic": 123456,
            "comment": "FIN close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(request)
        return result.retcode == mt5.TRADE_RETCODE_DONE

    def close_all_positions(self):
        """Закрывает ВСЕ открытые позиции."""
        self.connect()
        closed = 0
        for pos in mt5.positions_get():
            if self.close_position(pos.ticket):
                closed += 1
        print(f"[MT5] Closed {closed} positions")
        return closed

    def modify_sl(self, ticket, new_sl):
        """Modify SL for an open position by ticket."""
        self.connect()
        pos = mt5.positions_get(ticket=ticket)
        if not pos:
            print(f"[MT5] Position #{ticket} not found for SL modify")
            return False
        pos = pos[0]
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol": pos.symbol,
            "sl": new_sl,
            "tp": pos.tp,
        }
        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"[MT5] SL modify failed #{ticket}: {result.comment} (code {result.retcode})")
            return False
        print(f"[MT5] SL modified #{ticket} -> {new_sl}")
        return True

    def get_position_history(self, hours=24):
        """Get closed positions from the last N hours."""
        self.connect()
        from datetime import datetime, timedelta
        since = datetime.now() - timedelta(hours=hours)
        history = mt5.history_deals_get(since, datetime.now())
        if not history:
            return []
        deals = [d._asdict() for d in history]
        return deals

    def get_closed_trade_pnl(self, ticket, hours=72, symbol=None, entry_price=None):
        """Get realized P&L for a closed position by its position_id.
        NOTE: OANDA MT5 ignores the position= filter in history_deals_get,
        so we must fetch all deals and filter manually."""
        self.connect()
        from datetime import datetime, timedelta
        since = datetime.now() - timedelta(hours=hours)
        now = datetime.now()

        all_deals = mt5.history_deals_get(since, now)
        if not all_deals:
            return 0.0, 0.0, 0.0

        # Filter deals by position_id manually (position= param is broken in OANDA MT5)
        matching = [d for d in all_deals if d.position_id == ticket]
        if not matching:
            return 0.0, 0.0, 0.0

        # Verify entry price matches (safety check)
        if symbol and entry_price:
            entry_deals = [d for d in matching if d.entry == 0]
            if entry_deals:
                deal_entry = entry_deals[0].price
                if deal_entry > 0 and abs(deal_entry - entry_price) > entry_price * 0.02:
                    return 0.0, 0.0, 0.0

        pnl = sum(d.profit for d in matching)
        exit_deals = [d for d in matching if d.entry == 1]
        exit_price = exit_deals[-1].price if exit_deals else 0
        volume = max((d.volume for d in matching), default=0)
        return pnl, exit_price, volume

    def get_positions_summary(self):
        """Позиции + P&L."""
        positions = self.get_positions()
        total_pnl = sum(p.get("profit", 0) for p in positions)
        return {"positions": positions, "total_pnl": round(total_pnl, 2), "count": len(positions)}

    def get_account_summary(self):
        """Баланс, equity."""
        with self._lock:
            self.connect()
            info = mt5.account_info()
            if info is None:
                return {"balance": 0, "equity": 0}
            return {"balance": info.balance, "equity": info.equity}

    # --- Symbol helpers ---

    def get_symbol_info(self, symbol):
        """Возвращает contract_size, point и т.д."""
        self.connect()
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        return info._asdict()


# Синглтон
client = MT5Client()
