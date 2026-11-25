import json
import logging
import os
import threading
import time

import config
import telegram_bot as tg
from bingx import BingX, BingXError

log = logging.getLogger("trader")


def _px(v):
    if v is None:
        return "—"
    return f"{v:.8g}" if v < 1 else f"{v:,.4f}".rstrip("0").rstrip(".")


class Trader:
    def __init__(self, bx: BingX):
        self.bx = bx
        self.lock = threading.Lock()
        self.state = {"trades": {}, "seen": []}
        if os.path.exists(config.STATE_FILE):
            try:
                with open(config.STATE_FILE) as f:
                    self.state.update(json.load(f))
            except Exception as e:  # noqa
                log.warning("No se pudo leer el estado: %s", e)

    # ---------- persistencia ----------
    def save(self):
        try:
            with open(config.STATE_FILE, "w") as f:
                json.dump(self.state, f)
        except Exception as e:  # noqa
            log.warning("No se pudo guardar el estado: %s", e)

    def seen(self, key):
        return key in self.state["seen"]

    def mark_seen(self, key):
        self.state["seen"].append(key)
        self.state["seen"] = self.state["seen"][-3000:]
        self.save()

    def open_count(self):
        return len(self.state["trades"])

    def has_trade(self, symbol):
        return symbol in self.state["trades"]

    # ---------- apertura ----------
    def open_trade(self, symbol, sg):
        """Devuelve (ok, info_dict_o_mensaje)."""
        bx = self.bx
        d = sg["dir"]
        long = d == 1
        entry_side, exit_side = ("BUY", "SELL") if long else ("SELL", "BUY")
        pos_side = "LONG" if long else "SHORT"

        equity, avail = bx.equity()
        risk_usdt = equity * config.RISK_PCT / 100.0
        risk_per_unit = abs(sg["entry"] - sg["sl"])
        if risk_per_unit <= 0:
            return False, "riesgo por unidad = 0"
        px_now = bx.price(symbol)
        # el precio no debe haberse escapado de la entrada
        slip = (px_now - sg["entry"]) if long else (sg["entry"] - px_now)
        if slip > config.MAX_SLIPPAGE_R * risk_per_unit:
            return False, f"precio ya se alejó de la entrada ({_px(px_now)} vs {_px(sg['entry'])})"
        if (long and (px_now <= sg["sl"] or px_now >= sg["tp1"])) or ((not long) and (px_now >= sg["sl"] or px_now <= sg["tp1"])):
            return False, "precio fuera del rango SL–TP1"

        qty = risk_usdt / risk_per_unit
        max_qty = (avail * config.LEVERAGE * 0.9) / px_now
        capped = qty > max_qty
        qty = min(qty, max_qty)
        qty = bx.fmt_qty(symbol, qty)
        mq = bx.min_qty(symbol)
        if qty <= 0 or qty < mq:
            return False, f"cantidad {qty} menor al mínimo {mq} (saldo insuficiente para el riesgo)"

        qty1 = bx.fmt_qty(symbol, qty * config.TP1_PCT / 100.0)
        if qty1 < mq or qty - qty1 < mq:
            qty1 = 0.0
        qty2 = bx.fmt_qty(symbol, qty - qty1)

        bx.prepare_symbol(symbol, config.LEVERAGE)
        entry_id = bx.order(symbol, entry_side, pos_side, "MARKET", qty)
        ids = {"entry": entry_id}
        sl_p = bx.fmt_price(symbol, sg["sl"])
        try:
            ids["sl"] = bx.order(symbol, exit_side, pos_side, "STOP_MARKET", qty, sl_p)
        except BingXError as e:
            # sin stop no se queda abierto: cerrar de inmediato
            try:
                bx.order(symbol, exit_side, pos_side, "MARKET", qty)
            except BingXError:
                pass
            return False, f"no se pudo colocar el SL, posición cerrada: {e}"
        try:
            if qty1 > 0:
                ids["tp1"] = bx.order(symbol, exit_side, pos_side, "TAKE_PROFIT_MARKET", qty1,
                                      bx.fmt_price(symbol, sg["tp1"]))
            ids["tp2"] = bx.order(symbol, exit_side, pos_side, "TAKE_PROFIT_MARKET", qty2,
                                  bx.fmt_price(symbol, sg["tp2"]))
        except BingXError as e:
            tg.send(f"⚠️ <b>{symbol}</b>: error colocando TP ({e}). El SL sí está activo, revisa la posición.")

        trade = dict(symbol=symbol, dir=d, entry=sg["entry"], sl=sl_p, tp1=sg["tp1"], tp2=sg["tp2"],
                     qty=qty, qty1=qty1, qty2=qty2, ids=ids, tp1_done=False, opened=int(time.time()))
        with self.lock:
            self.state["trades"][symbol] = trade
            self.save()
        return True, dict(qty=qty, qty1=qty1, qty2=qty2, risk_usdt=risk_usdt, equity=equity,
                          notional=qty * px_now, margin=qty * px_now / config.LEVERAGE,
                          capped=capped, ids=ids)

    # ---------- seguimiento ----------
    def monitor(self):
        if not self.state["trades"]:
            return
        bx = self.bx
        try:
            pos = bx.positions()
        except BingXError as e:
            log.warning("positions: %s", e)
            return
        for sym, t in list(self.state["trades"].items()):
            side = "LONG" if t["dir"] == 1 else "SHORT"
            amt = 0.0
            for p in pos:
                if p.get("symbol") == sym and (p.get("positionSide") in (side, "BOTH")):
                    amt += abs(float(p.get("positionAmt", 0) or 0))
            try:
                if amt > 0:
                    self._manage_open(sym, t, amt)
                else:
                    self._finalize(sym, t)
            except BingXError as e:
                log.warning("monitor %s: %s", sym, e)

    def _status(self, sym, oid):
        if not oid:
            return None
        try:
            return str(self.bx.get_order(sym, oid).get("status", "")).upper()
        except BingXError:
            return None

    def _manage_open(self, sym, t, amt):
        bx = self.bx
        if config.MOVE_BE and not t["tp1_done"] and t["ids"].get("tp1"):
            if self._status(sym, t["ids"]["tp1"]) == "FILLED":
                long = t["dir"] == 1
                exit_side, pos_side = ("SELL", "LONG") if long else ("BUY", "SHORT")
                bx.cancel(sym, t["ids"]["sl"])
                be = bx.fmt_price(sym, t["entry"])
                qty = bx.fmt_qty(sym, amt)
                t["ids"]["sl"] = bx.order(sym, exit_side, pos_side, "STOP_MARKET", qty, be)
                t["sl"] = be
                t["tp1_done"] = True
                with self.lock:
                    self.save()
                tg.send(f"✅ <b>{sym}</b> TP1 alcanzado ({_px(t['tp1'])}). SL movido a break-even {_px(be)}. "
                        f"Restante: {qty} hacia TP2 {_px(t['tp2'])}")

    def _finalize(self, sym, t):
        bx = self.bx
        ids = t["ids"]
        st = {k: self._status(sym, ids.get(k)) for k in ("sl", "tp1", "tp2")}
        bx.cancel_all(sym)
        if st.get("tp2") == "FILLED":
            res = "🏆 <b>TP2 alcanzado</b> — QM completo"
        elif st.get("sl") == "FILLED":
            res = "🛡️ SL en break-even" if t["tp1_done"] else "❌ <b>Stop loss</b>"
            if t["tp1_done"]:
                res += " (tras TP1)"
        else:
            res = "ℹ️ Posición cerrada (manual o liquidación)"
        tg.send(f"{res}\n<b>{sym}</b> {'LONG' if t['dir'] == 1 else 'SHORT'}\n"
                f"Entrada {_px(t['entry'])} · SL {_px(t['sl'])} · TP1 {_px(t['tp1'])} · TP2 {_px(t['tp2'])}\n"
                f"Estados: SL={st['sl']} TP1={st['tp1']} TP2={st['tp2']}")
        with self.lock:
            self.state["trades"].pop(sym, None)
            self.save()
