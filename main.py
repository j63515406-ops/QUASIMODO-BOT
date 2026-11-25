import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

import config
import telegram_bot as tg
from bingx import BingX, BingXError
from strategy import Params, run_qm
from trader import Trader, _px

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")


def _i_raw(name):
    import os
    try:
        return int(float(os.getenv(name, "0") or 0))
    except ValueError:
        return 0

bx = BingX()
trader = Trader(bx)
params = Params(**config.STRATEGY)
TF_MS = config.TF_SECONDS.get(config.TIMEFRAME, 900) * 1000


def to_df(kl, tf_ms):
    df = pd.DataFrame(kl)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col])
    df["time"] = pd.to_numeric(df["time"])
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
    # descartar la vela en formación
    now_ms = int(time.time() * 1000)
    df = df[df["time"] + tf_ms <= now_ms].reset_index(drop=True)
    return df


def universe():
    cs = bx.contracts()
    try:
        tk = {t["symbol"]: t for t in bx.tickers()}
    except BingXError:
        tk = {}
    out = []
    for c in cs:
        s = c.get("symbol", "")
        if not s.endswith("-USDT") or int(c.get("status", 1)) != 1:
            continue
        base = s.split("-")[0].upper()
        if any(s.upper().startswith(p) for p in config.EXCLUDE_PREFIX):
            continue
        if config.WHITELIST and base not in config.WHITELIST and s.upper() not in config.WHITELIST:
            continue
        if base in config.BLACKLIST or s.upper() in config.BLACKLIST:
            continue
        t = tk.get(s, {})
        try:
            qv = float(t.get("quoteVolume") or 0) or float(t.get("volume") or 0) * float(t.get("lastPrice") or 0)
        except Exception:  # noqa
            qv = 0
        if tk and qv < config.MIN_QUOTE_VOLUME:
            continue
        out.append(s)
    return sorted(out)


def analyze(sym):
    try:
        df = to_df(bx.klines(sym, config.TIMEFRAME, config.CANDLES), TF_MS)
        if len(df) < 120:
            return None
        htf_up = True
        if config.USE_HTF:
            htf_ms = config.TF_SECONDS.get(config.HTF_TIMEFRAME, 14400) * 1000
            dh = to_df(bx.klines(sym, config.HTF_TIMEFRAME, max(config.HTF_LEN * 3, 150)), htf_ms)
            if len(dh) < config.HTF_LEN:
                return None
            ema = dh["close"].ewm(span=config.HTF_LEN, adjust=False).mean().iloc[-1]
            htf_up = dh["close"].iloc[-1] > ema
        res = run_qm(df, params, htf_up=htf_up, use_htf=config.USE_HTF)
        if not res:
            return None
        res["symbol"] = sym
        res["bar_time"] = int(df["time"].iloc[-1])
        return res
    except Exception as e:  # noqa
        log.debug("analyze %s: %s", sym, e)
        return None


def fmt_signal(sym, sg, mode_txt, extra=""):
    long = sg["dir"] == 1
    risk_pct = abs(sg["entry"] - sg["sl"]) / sg["entry"] * 100
    chk = lambda b: "✅" if b else "❌"  # noqa
    div = "✅" + (" RSI" if sg["div_rsi"] else "") + (" CVD" if sg["div_cvd"] else "") if sg["div"] else "❌"
    rv = f" x{sg['rvol']:.1f}" if sg["rvol"] else ""
    return (
        f"{'🟢▲ LONG' if long else '🔴▼ SHORT'} · <b>{sym}</b> · {config.TIMEFRAME}\n"
        f"Patrón: Quasimodo {'alcista' if long else 'bajista'} (barrida + BOS + QML) · entrada "
        f"{'al tocar' if sg['mode'] == 'touch' else 'con vela de confirmación'}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📍 Entrada: <code>{_px(sg['entry'])}</code>\n"
        f"🛑 SL: <code>{_px(sg['sl'])}</code> (riesgo {risk_pct:.2f}%)\n"
        f"🎯 TP1: <code>{_px(sg['tp1'])}</code> · R:R 1:{sg['rr1']:.1f}\n"
        f"🏁 TP2: <code>{_px(sg['tp2'])}</code> · R:R 1:{sg['rr2']:.1f}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧩 Niveles: QML {_px(sg['qml'])} · Cabeza {_px(sg['head'])} · BOS {_px(sg['bos'])} · ATR {sg['atr'] / sg['entry'] * 100:.2f}%\n"
        f"📊 Confluencia {sg['score']}/{sg['max_score']} (mín. {params.min_score})\n"
        f"  {chk(sg['st'])} Supertrend a favor\n"
        f"  {chk(sg['rsi_ok'])} Giro RSI ({sg['rsi']:.0f})\n"
        f"  {div} Divergencia en la cabeza\n"
        f"  {chk(sg['cvd'])} Flujo CVD (estimado)\n"
        f"  {chk(sg['vol'])} Volumen relativo{rv}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{mode_txt}{extra}"
    )


def process_signal(sym, sg):
    key = f"{sym}|{config.TIMEFRAME}|{sg['bar']}|{sg['dir']}|{sg['bar_time']}"
    if trader.seen(key):
        return
    trader.mark_seen(key)
    if config.SIDE_FILTER == "LONG" and sg["dir"] != 1:
        return
    if config.SIDE_FILTER == "SHORT" and sg["dir"] != -1:
        return

    if config.DRY_RUN:
        tg.send(fmt_signal(sym, sg, "📝 <b>MODO PAPEL</b> (DRY_RUN=true): no se envió ninguna orden"))
        return
    if trader.has_trade(sym):
        tg.send(fmt_signal(sym, sg, "⏭️ Señal omitida: ya hay una posición abierta en este símbolo"))
        return
    if trader.open_count() >= config.MAX_OPEN:
        tg.send(fmt_signal(sym, sg, f"⏭️ Señal omitida: máximo de {config.MAX_OPEN} posiciones abiertas"))
        return
    try:
        ok, info = trader.open_trade(sym, sg)
    except Exception as e:  # noqa
        ok, info = False, f"error: {e}"
    if ok:
        extra = (f"💰 Equity {info['equity']:.2f} USDT · riesgo {info['risk_usdt']:.2f} USDT ({config.RISK_PCT}%)\n"
                 f"📦 Cantidad {info['qty']} (TP1 {info['qty1']} / TP2 {info['qty2']}) · nominal {info['notional']:.2f} · "
                 f"margen {info['margin']:.2f} · x{config.LEVERAGE} aislado"
                 + (" · ⚠️ limitada por margen" if info["capped"] else "") + "\n"
                 f"🆔 Órdenes: " + ", ".join(f"{k}={v}" for k, v in info["ids"].items()))
        tg.send(fmt_signal(sym, sg, "⚡ <b>OPERADO EN BINGX</b>\n", extra))
    else:
        tg.send(fmt_signal(sym, sg, "⛔ <b>NO operada</b>: ", str(info)))


def scan_cycle():
    t0 = time.time()
    syms = universe()
    log.info("Escaneando %d símbolos (%s)", len(syms), config.TIMEFRAME)
    with ThreadPoolExecutor(max_workers=config.WORKERS) as ex:
        results = [r for r in ex.map(analyze, syms) if r]
    sigs, setups, forming = [], [], 0
    for r in results:
        ev = r["ev"]
        if r["stage"] >= 3:
            forming += 1
        if ev["sig"]:
            sigs.append((r["symbol"], dict(ev["sig"], bar_time=r["bar_time"])))
        if config.NOTIFY_SETUPS and (ev["bos"] or ev["tap"]):
            setups.append(r)
    sigs.sort(key=lambda x: (x[1]["score"], x[1]["rr2"]), reverse=True)
    for sym, sg in sigs:
        try:
            process_signal(sym, sg)
        except Exception:  # noqa
            log.error("process_signal %s\n%s", sym, traceback.format_exc())
    for r in setups:
        ev = r["ev"]
        what = "QM formado (BOS)" if ev["bos"] else "QML tocado"
        tg.send(f"👀 <b>{r['symbol']}</b> {what} · {'▲ alcista' if r['dir'] == 1 else '▼ bajista'} · {config.TIMEFRAME}\n"
                f"QML {_px(r['qml'])} · Cabeza {_px(r['head'])} · BOS {_px(r['bos'])}")
    report_cycle(results, sigs, time.time() - t0)


def report_cycle(results, sigs, secs):
    from collections import Counter
    waiting = []          # stage 3: QM formado esperando el QML
    touched = Counter()   # stage 4: QML tocado, motivo por el que aún no hay señal
    new_bos = new_tap = 0
    for r in results:
        ev = r["ev"]
        new_bos += ev["bos"]
        new_tap += ev["tap"]
        if r["stage"] == 3 and r["atr"] > 0:
            waiting.append((abs(r["close"] - r["zfar"]) / r["atr"], r["symbol"], r["dir"]))
        elif r["stage"] == 4:
            if r["sig_done"]:
                touched["señal ya emitida / en curso"] += 1
            else:
                touched[ev["block"] or "evaluando"] += 1
    waiting.sort()
    n3 = len(waiting)
    n4 = sum(touched.values())
    log.info("Ciclo %.0fs · analizados %d · QM formados esperando QML: %d · QML tocados: %d · "
             "nuevos BOS: %d · nuevos toques: %d · SEÑALES: %d",
             secs, len(results), n3, n4, new_bos, new_tap, len(sigs))
    if waiting:
        log.info("  más cerca del QML: " + ", ".join(
            f"{s} {'▲' if d == 1 else '▼'} {x:.1f}ATR" for x, s, d in waiting[:5]))
    if touched:
        log.info("  tocados sin señal: " + " · ".join(f"{k}: {v}" for k, v in touched.most_common()))
    if config.NOTIFY_SUMMARY:
        lines = [f"📡 <b>Resumen {config.TIMEFRAME}</b> · {len(results)} monedas · señales {len(sigs)}",
                 f"🆕 BOS nuevos {new_bos} · toques nuevos {new_tap}",
                 f"⏳ Esperando QML: {n3}"]
        for x, s, d in waiting[:8]:
            lines.append(f"  {'▲' if d == 1 else '▼'} {s} a {x:.1f} ATR del QML")
        lines.append(f"🎯 QML tocado sin señal: {n4}")
        for k, v in touched.most_common():
            lines.append(f"  {v}× {k}")
        tg.send("\n".join(lines))


def monitor_loop():
    while True:
        try:
            trader.monitor()
        except Exception:  # noqa
            log.error("monitor\n%s", traceback.format_exc())
        time.sleep(config.MONITOR_SECS)


def main():
    if _i_raw("MIN_SCORE") > 5:
        log.warning("MIN_SCORE=%s es imposible (máx. 5); se usa 5", _i_raw("MIN_SCORE"))
    mode = "📝 PAPEL (solo avisos)" if config.DRY_RUN else "⚡ REAL"
    if not config.DRY_RUN:
        if not (config.BINGX_API_KEY and config.BINGX_API_SECRET):
            log.error("Faltan BINGX_API_KEY / BINGX_API_SECRET")
            sys.exit(1)
        bx.set_hedge()
    tg.send(f"🤖 <b>Bot Quasimodo BingX iniciado</b>\nModo: {mode}\nTimeframe: {config.TIMEFRAME} · "
            f"riesgo {config.RISK_PCT}% · x{config.LEVERAGE} · máx. {config.MAX_OPEN} posiciones\n"
            f"Confluencia mín. {params.min_score}/5 · entrada: {params.sig_mode} · HTF: {'sí' if config.USE_HTF else 'no'}")
    if not config.DRY_RUN:
        threading.Thread(target=monitor_loop, daemon=True).start()

    tf = config.TF_SECONDS.get(config.TIMEFRAME, 900)
    errors = 0
    while True:
        now = time.time()
        wake = (int(now // tf) + 1) * tf + config.CLOSE_DELAY_SECS
        time.sleep(max(1, wake - now))
        try:
            scan_cycle()
            errors = 0
        except Exception as e:  # noqa
            errors += 1
            log.error("scan_cycle\n%s", traceback.format_exc())
            if errors in (1, 5, 20):
                tg.send(f"⚠️ Error en el ciclo de escaneo ({errors}): {e}")


if __name__ == "__main__":
    main()
