import hashlib
import hmac
import logging
import threading
import time

import requests

import config

log = logging.getLogger("bingx")


class RateLimiter:
    def __init__(self, per_sec):
        self.gap = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            now = time.time()
            if now < self.next:
                time.sleep(self.next - now)
                now = time.time()
            self.next = now + self.gap


class BingXError(Exception):
    pass


class BingX:
    def __init__(self):
        self.base = config.BINGX_BASE_URL
        self.key = config.BINGX_API_KEY
        self.secret = config.BINGX_API_SECRET
        self.rl = RateLimiter(config.RATE_PER_SEC)
        self.s = requests.Session()
        self._contracts = {}

    # ---------- HTTP ----------
    def _req(self, method, path, params=None, signed=False, retries=3):
        params = dict(params or {})
        for attempt in range(retries):
            self.rl.wait()
            p = dict(params)
            headers = {}
            if signed:
                p["timestamp"] = int(time.time() * 1000)
                p["recvWindow"] = 10000
                qs = "&".join(f"{k}={p[k]}" for k in sorted(p))
                sig = hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
                url = f"{self.base}{path}?{qs}&signature={sig}"
                headers["X-BX-APIKEY"] = self.key
            else:
                qs = "&".join(f"{k}={p[k]}" for k in sorted(p))
                url = f"{self.base}{path}" + (f"?{qs}" if qs else "")
            try:
                r = self.s.request(method, url, headers=headers, timeout=15)
                j = r.json()
            except Exception as e:  # noqa
                if attempt == retries - 1:
                    raise BingXError(f"red/JSON: {e}")
                time.sleep(1.5 * (attempt + 1))
                continue
            code = j.get("code", 0)
            if code == 0:
                return j.get("data")
            if code in (100410, 429) or r.status_code == 429:  # rate limit
                time.sleep(2 * (attempt + 1))
                continue
            raise BingXError(f"{path} code={code} msg={j.get('msg')}")
        raise BingXError(f"{path}: reintentos agotados")

    # ---------- mercado ----------
    def contracts(self):
        data = self._req("GET", "/openApi/swap/v2/quote/contracts")
        self._contracts = {c["symbol"]: c for c in data}
        return data

    def contract(self, symbol):
        if symbol not in self._contracts:
            self.contracts()
        return self._contracts.get(symbol, {})

    def tickers(self):
        return self._req("GET", "/openApi/swap/v2/quote/ticker")

    def klines(self, symbol, interval, limit, end_time=None):
        prm = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time:
            prm["endTime"] = int(end_time)
        try:
            data = self._req("GET", "/openApi/swap/v3/quote/klines", prm)
        except BingXError:
            data = self._req("GET", "/openApi/swap/v2/quote/klines", prm)
        data = sorted(data, key=lambda k: int(k["time"]))
        return data

    def price(self, symbol):
        d = self._req("GET", "/openApi/swap/v2/quote/price", {"symbol": symbol})
        if isinstance(d, list):
            d = d[0]
        return float(d["price"])

    # ---------- cuenta ----------
    def equity(self):
        d = self._req("GET", "/openApi/swap/v2/user/balance", signed=True)
        b = d["balance"] if isinstance(d, dict) else d[0]
        return float(b.get("equity") or b.get("balance")), float(b.get("availableMargin", 0))

    def positions(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._req("GET", "/openApi/swap/v2/user/positions", p, signed=True) or []

    def set_hedge(self):
        try:
            self._req("POST", "/openApi/swap/v1/positionSide/dual",
                      {"dualSidePosition": "true" if config.HEDGE_MODE else "false"}, signed=True)
        except BingXError as e:
            log.info("positionSide/dual: %s", e)

    def prepare_symbol(self, symbol, leverage):
        try:
            self._req("POST", "/openApi/swap/v2/trade/marginType",
                      {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BingXError as e:
            log.info("marginType %s: %s", symbol, e)
        sides = ["LONG", "SHORT"] if config.HEDGE_MODE else ["BOTH"]
        for sd in sides:
            try:
                self._req("POST", "/openApi/swap/v2/trade/leverage",
                          {"symbol": symbol, "side": sd, "leverage": leverage}, signed=True)
            except BingXError as e:
                log.info("leverage %s %s: %s", symbol, sd, e)

    # ---------- ordenes ----------
    def order(self, symbol, side, pos_side, otype, qty, stop_price=None):
        p = {"symbol": symbol, "side": side, "type": otype, "quantity": qty}
        if config.HEDGE_MODE:
            p["positionSide"] = pos_side
        else:
            p["positionSide"] = "BOTH"
            if otype != "MARKET":
                p["reduceOnly"] = "true"
        if stop_price is not None:
            p["stopPrice"] = stop_price
            p["workingType"] = "MARK_PRICE"
        d = self._req("POST", "/openApi/swap/v2/trade/order", p, signed=True)
        o = d.get("order", d) if isinstance(d, dict) else d
        return str(o.get("orderId"))

    def get_order(self, symbol, order_id):
        d = self._req("GET", "/openApi/swap/v2/trade/order",
                      {"symbol": symbol, "orderId": order_id}, signed=True)
        return (d.get("order", d) if isinstance(d, dict) else d)

    def cancel(self, symbol, order_id):
        try:
            self._req("DELETE", "/openApi/swap/v2/trade/order",
                      {"symbol": symbol, "orderId": order_id}, signed=True)
        except BingXError as e:
            log.info("cancel %s %s: %s", symbol, order_id, e)

    def cancel_all(self, symbol):
        try:
            self._req("DELETE", "/openApi/swap/v2/trade/allOpenOrders", {"symbol": symbol}, signed=True)
        except BingXError as e:
            log.info("cancel_all %s: %s", symbol, e)

    # ---------- utilidades de redondeo ----------
    def fmt_qty(self, symbol, qty):
        prec = int(self.contract(symbol).get("quantityPrecision", 0))
        f = 10 ** prec
        return int(qty * f + 1e-9) / f

    def fmt_price(self, symbol, price):
        prec = int(self.contract(symbol).get("pricePrecision", 4))
        return round(price, prec)

    def min_qty(self, symbol):
        c = self.contract(symbol)
        try:
            return float(c.get("tradeMinQuantity", 0) or 0)
        except Exception:  # noqa
            return 0.0
