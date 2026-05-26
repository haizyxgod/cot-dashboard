"""OANDA v20 REST API Client — свечи и ордера."""
import requests
import pandas as pd
import config


class OandaClient:
    """OANDA API wrapper для получения свечей и исполнения ордеров."""

    def __init__(self):
        self.base = config.OANDA_URL
        self.account = config.OANDA_ACCOUNT_ID
        self.headers = {
            "Authorization": f"Bearer {config.OANDA_API_KEY}",
            "Content-Type": "application/json",
        }

    def _get(self, path, params=None):
        r = requests.get(f"{self.base}{path}", headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def _post(self, path, body):
        r = requests.post(f"{self.base}{path}", headers=self.headers, json=body, timeout=15)
        r.raise_for_status()
        return r.json()

    # --- Candles ---

    def get_candles(self, instrument, granularity, count=50):
        """
        Получает свечи.
        granularity: 'D', 'H4', 'H1', 'M15'
        Returns: pandas DataFrame with columns [time, open, high, low, close, volume]
        """
        path = f"/accounts/{self.account}/instruments/{instrument}/candles"
        params = {
            "granularity": granularity,
            "count": count,
            "price": "M",  # Midpoint candles
        }
        data = self._get(path, params)
        rows = []
        for c in data.get("candles", []):
            mid = c.get("mid", {})
            rows.append(
                {
                    "time": pd.Timestamp(c["time"]),
                    "open": float(mid["o"]),
                    "high": float(mid["h"]),
                    "low": float(mid["l"]),
                    "close": float(mid["c"]),
                    "volume": int(c.get("volume", 0)),
                }
            )
        return pd.DataFrame(rows).sort_values("time").reset_index(drop=True)

    def get_current_price(self, instrument):
        """Возвращает текущий ask/bid."""
        path = f"/accounts/{self.account}/pricing"
        params = {"instruments": instrument}
        data = self._get(path, params)
        price = data["prices"][0]
        return {
            "bid": float(price["bids"][0]["price"]),
            "ask": float(price["asks"][0]["price"]),
        }

    # --- Orders ---

    def place_stop_order(self, instrument, direction, price, sl, tp, units):
        """
        Ставит отложенный STOP-ордер.
        direction: 'BUY' or 'SELL'
        price: уровень входа (триггер)
        sl: stop loss
        tp: take profit
        units: количество единиц (+ для buy, - для sell)
        """
        if direction == "SELL":
            units = -abs(units)
        else:
            units = abs(units)

        body = {
            "order": {
                "type": "STOP",
                "instrument": instrument,
                "units": str(units),
                "price": str(round(price, 5)),
                "stopLossOnFill": {"price": str(round(sl, 5))},
                "takeProfitOnFill": {"price": str(round(tp, 5))},
                "timeInForce": "GTC",
            }
        }
        return self._post(f"/accounts/{self.account}/orders", body)

    def get_positions(self):
        """Возвращает список открытых позиций."""
        path = f"/accounts/{self.account}/openPositions"
        return self._get(path).get("positions", [])

    def get_account_summary(self):
        """Баланс, equity, маржа."""
        path = f"/accounts/{self.account}/summary"
        return self._get(path).get("account", {})
