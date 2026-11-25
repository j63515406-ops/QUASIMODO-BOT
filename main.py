import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config
import telegram_bot as tg
from bingx import BingX, BingXError, to_df
from strategy import Params, cost_r, run_qm
from trader import Trader, _px

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

bx = BingX()
trader = Trader(bx)
params = Params(**config.STRATEGY)
sig_lock = threading.Lock()          # varias temporalidades comparten el estado del trader
_uni = {"t": 0.0, "syms": []}
_uni_lock = threading.Lock()
_last_radar = {}
_last_warn = {}


def tf_ms(tf):
    return config.TF_SECONDS[tf] * 1000


def hhmm(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%H:%M UTC")


# ---------------------------------------------------------------- universo
def universe():
    with _uni_lock:
        if _uni["syms"] and time.time() - _uni["t"] < config.UNIVERSE_TTL:
            return list(_uni["syms"])
        try:
            cs = bx.contracts()
            try:
                tk = {t["symbol"]: t for t in bx.tickers()}
            except BingXError:
                tk = {}
        except Exception:  # noqa
            if _uni["syms"]:
                return list(_uni["syms"])      # usa la lista anterior si la API falla un momento
            raise
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
        out.sort()
        _uni.update(t=time.time(), syms=out)
        return list(out)


# ---------------------------------------------------------------- analisis
def analyze(sym, tf):
    """Devuelve None (sin datos suficientes), {'error': ...} o el resultado de run_qm."""
    try:
        df = to_df(bx.klines(sym, tf, config.CANDLES), tf_ms(tf))
        if len(df) < 120:
            return None
        htf_up = True
        use_htf = config.USE_HTF and config.TF_SECONDS[tf] < config.TF_SECONDS.get(config.HTF_TIMEFRAME, 14400)
        if use_htf:
            dh = to_df(bx.klines(sym, config.HTF_TIMEFRAME, max(config.HTF_LEN * 3, 150)),
                       config.TF_SECONDS.get(config.HTF_TIMEFRAME, 14400) * 1000)
            if len(dh) < config.HTF_LEN:
                return None
            ema = dh["close"].ewm(span=config.HTF_LEN, adjust=False).mean().iloc[-1]
            htf_up = dh["close"].iloc[-1] > ema
        res = run_qm(df, params, htf_up=htf_up, use_htf=use_htf)
        if not res:
            return None
        res["symbol"] = sym
        res["tf"] = tf
        res["times"] = df["time"].values
        return res
    except Exception as e:  # noqa
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- mensajes
def grade(score):
    return "A" if score >= 4 else "B" if score == 3 else "C"


def fmt_signal(sym, tf, sg, mode_txt, extra=""):
    long = sg["dir"] == 1
    risk_pct = abs(sg["entry"] - sg["sl"]) / sg["entry"] * 100
    cr = cost_r(sg["entry"], sg["sl"], config.FEE_PCT, config.SLIP_PCT)
    chk = lambda b: "✅" if b else "❌"  # noqa
    div = "✅" + (" RSI" if sg["div_rsi"] else "") + (" CVD" if sg["div_cvd"] else "") if sg["div"] else "❌"
    rv = f" x{sg['rvol']:.1f}" if sg["rvol"] else ""
    return (
        f"{'🟢▲ LONG' if long else '🔴▼ SHORT'} · <b>{sym}</b> · {tf} · calidad <b>{grade(sg['score'])}</b>\n"
        f"Patrón: Quasimodo {'alcista' if long else 'bajista'} (barrida + BOS + QML) · entrada "
        f"{'al tocar' if sg['mode'] == 'touch' else 'con vela de confirmación'} · vela cerrada {hhmm(sg['bar_time'] + tf_ms(tf))}\n"
        f"━━━━━━━━━━━━━━\n"
        f"📍 Entrada: <code>{_px(sg['entry'])}</code>\n"
        f"🛑 SL: <code>{_px(sg['sl'])}</code> (riesgo {risk_pct:.2f}% · coste fees+slip ≈ {cr:.2f}R)\n"
        f"🎯 TP1: <code>{_px(sg['tp1'])}</code> · R:R 1:{sg['rr1']:.1f}\n"
        f"🏁 TP2: <code>{_px(sg['tp2'])}</code> · R:R 1:{sg['rr2']:.1f}\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧩 Niveles: QML {_px(sg['qml'])} · Cabeza {_px(sg['head'])} · BOS {_px(sg['bos'])} · ATR {sg['atr'] / sg['entry'] * 100:.2f}%\n"
        f"📊 Confluencia {sg['score']}/{sg['max_score']} (mín. {params.min_score} para avisar · {config.TRADE_MIN_SCORE} para operar)\n"
        f"  {chk(sg['st'])} Supertrend a favor\n"
        f"  {chk(sg['rsi_ok'])} Giro RSI ({sg['rsi']:.0f})\n"
        f"  {div} Divergencia en la cabeza\n"
        f"  {chk(sg['cvd'])} Flujo CVD (estimado)\n"
        f"  {chk(sg['vol'])} Volumen relativo{rv}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{mode_txt}{extra}"
    )


def process_signal(sym, tf, sg):
    key = f"SIG|{sym}|{tf}|{sg['dir']}|{sg['head_time']}"
    with sig_lock:
        if trader.seen(key):
            return False
        trader.mark_seen(key)
        if config.SIDE_FILTER == "LONG" and sg["dir"] != 1:
            return False
        if config.SIDE_FILTER == "SHORT" and sg["dir"] != -1:
            return False

        if config.DRY_RUN:
            tg.send(fmt_signal(sym, tf, sg, "📝 <b>MODO PAPEL</b> (DRY_RUN=true): no se envió ninguna orden"))
            return True
        if sg["score"] < config.TRADE_MIN_SCORE:
            tg.send(fmt_signal(sym, tf, sg, f"ℹ️ <b>Solo aviso</b>: confluencia {sg['score']} < {config.TRADE_MIN_SCORE} (TRADE_MIN_SCORE), no se opera"))
            return True
        if trader.has_trade(sym):
            tg.send(fmt_signal(sym, tf, sg, "⏭️ Señal omitida: ya hay una posición abierta en este símbolo"))
            return True
        if trader.open_count() >= config.MAX_OPEN:
            tg.send(fmt_signal(sym, tf, sg, f"⏭️ Señal omitida: máximo de {config.MAX_OPEN} posiciones abiertas"))
            return True
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
            tg.send(fmt_signal(sym, tf, sg, "⚡ <b>OPERADO EN BINGX</b>\n", extra))
        else:
            tg.send(fmt_signal(sym, tf, sg, "⛔ <b>NO operada</b>: ", str(info)))
        return True


def notify_setups(r, tf, sig_syms):
    """Avisos del patron (como las alertas del Pine): QM formado / QML tocado."""
    ev, sym = r["ev"], r["symbol"]
    pat = ev.get("pat")
    if not pat or not config.NOTIFY_SETUPS:
        return
    long = pat["dir"] == 1
    head_t = int(r["times"][pat["head"]])
    arrow = "▲ alcista" if long else "▼ bajista"
    if ev["bos"] and "bos" in config.NOTIFY_SETUPS:
        key = f"SET|bos|{sym}|{tf}|{head_t}"
        if not trader.seen(key):
            trader.mark_seen(key)
            dist = abs(r["close"] - r["zfar"]) / r["atr"]
            tg.send(f"👀 <b>{sym}</b> · {tf} · QM formado (BOS) {arrow}\n"
                    f"Esperando retroceso al QML <code>{_px(r['qml'])}</code> (a {dist:.1f} ATR) · "
                    f"cabeza <code>{_px(r['head'])}</code> (invalida si cierra más allá) · BOS <code>{_px(r['bos'])}</code>")
    if ev["tap"] and "tap" in config.NOTIFY_SETUPS and sym not in sig_syms:
        key = f"SET|tap|{sym}|{tf}|{head_t}"
        if not trader.seen(key):
            trader.mark_seen(key)
            tg.send(f"🎯 <b>{sym}</b> · {tf} · QML tocado {arrow}\n"
                    f"QML <code>{_px(r['qml'])}</code> · cabeza <code>{_px(r['head'])}</code> · T1 <code>{_px(r['tp1'])}</code> · "
                    f"T2 <code>{_px(r['tp2'])}</code>\nEsperando vela de confirmación para la señal de entrada.")


def notify_outcomes(r, tf):
    """T1 / T2 / cabeza rota de señales que YA se enviaron (solo en modo papel; en real avisa el monitor del trader)."""
    if not (config.NOTIFY_OUTCOMES and config.DRY_RUN):
        return
    ev, sym = r["ev"], r["symbol"]
    pat = ev.get("pat")
    if not pat or not (ev["hit"] or ev["fail"]):
        return
    head_t = int(r["times"][pat["head"]])
    if not trader.seen(f"SIG|{sym}|{tf}|{pat['dir']}|{head_t}"):
        return
    what = None
    if ev["hit"] == 1:
        what = "T1 alcanzado ✅"
    elif ev["hit"] == 2:
        what = "T2 alcanzado 🏆 · QM completo"
    elif ev["fail"]:
        what = "✕ Cabeza rota (invalidada)" + (" tras T1" if pat["hit"] >= 1 else "")
    key = f"OUT|{what}|{sym}|{tf}|{head_t}"
    if what and not trader.seen(key):
        trader.mark_seen(key)
        sg = pat.get("signal") or {}
        tg.send(f"{what}\n<b>{sym}</b> {'LONG' if pat['dir'] == 1 else 'SHORT'} · {tf}"
                + (f"\nEntrada {_px(sg.get('entry'))} · SL {_px(sg.get('sl'))} · TP1 {_px(sg.get('tp1'))} · TP2 {_px(sg.get('tp2'))}" if sg else ""))


def send_radar(tf, results, n_sigs, secs, n_syms, n_err):
    if config.RADAR_EVERY_MIN <= 0:
        return
    now = time.time()
    if now - _last_radar.get(tf, 0) < config.RADAR_EVERY_MIN * 60:
        return
    _last_radar[tf] = now
    wait = [r for r in results if r["stage"] == 3]
    play = [r for r in results if r["stage"] == 4]
    for r in wait:
        r["dist"] = abs(r["close"] - r["zfar"]) / r["atr"]
    wait.sort(key=lambda r: r["dist"])
    lines = [f"📡 <b>RADAR QM · {tf}</b> · {hhmm(now * 1000)}",
             f"Analizadas {len(results)}/{n_syms} monedas en {secs:.0f}s" + (f" · ⚠️ {n_err} con error" if n_err else ""),
             f"⏳ Esperando QML: {len(wait)} · 🎯 QML tocado (en juego): {len(play)} · ✅ Señales en este ciclo: {n_sigs}"]
    if wait:
        lines.append("Más cerca del QML:")
        for r in wait[:config.RADAR_TOP]:
            age = r["n"] - 1 - r["bos_idx"]
            lines.append(f"{'▲' if r['dir'] == 1 else '▼'} <b>{r['symbol']}</b> QML <code>{_px(r['qml'])}</code> · "
                         f"a {r['dist']:.1f} ATR · cabeza <code>{_px(r['head'])}</code> · BOS hace {age} velas")
    tg.send("\n".join(lines))


# ---------------------------------------------------------------- ciclo
def scan_cycle(tf):
    t0 = time.time()
    syms = universe()
    log.info("[%s] Escaneando %d símbolos", tf, len(syms))
    with ThreadPoolExecutor(max_workers=config.WORKERS) as ex:
        raw = list(ex.map(lambda s: analyze(s, tf), syms))
    errs = [r["error"] for r in raw if r and "error" in r]
    results = [r for r in raw if r and "error" not in r]

    # --- la API no responde / bloquea: avisar (antes los errores se tragaban en silencio)
    if syms and len(errs) > 0.5 * len(syms) and time.time() - _last_warn.get(tf, 0) > 3600:
        _last_warn[tf] = time.time()
        tg.send(f"⚠️ <b>{tf}</b>: falló el análisis de {len(errs)}/{len(syms)} monedas. Ejemplo: {errs[0][:200]}\n"
                f"Revisa BINGX_BASE_URL / región de Railway / límites de la API.")
    if errs:
        log.warning("[%s] %d errores de análisis, ej: %s", tf, len(errs), errs[0])

    # --- señales (últimas SIGNAL_LOOKBACK velas) + filtro de coste
    sigs, dropped_cost = [], 0
    for r in results:
        for sg in r["signals"]:
            if sg["bar"] < r["n"] - config.SIGNAL_LOOKBACK:
                continue
            if cost_r(sg["entry"], sg["sl"], config.FEE_PCT, config.SLIP_PCT) > config.MAX_COST_R:
                dropped_cost += 1
                continue
            sg = dict(sg, bar_time=int(r["times"][sg["bar"]]), head_time=int(r["times"][sg["head_idx"]]))
            sigs.append((r["symbol"], sg))
    sigs.sort(key=lambda x: (x[1]["score"], x[1]["rr2"]), reverse=True)
    sent = 0
    for sym, sg in sigs[:config.MAX_SIGNALS_PER_CYCLE]:
        try:
            if process_signal(sym, tf, sg):
                sent += 1
        except Exception:  # noqa
            log.error("process_signal %s\n%s", sym, traceback.format_exc())

    sig_syms = {s for s, _ in sigs}
    for r in results:
        try:
            notify_setups(r, tf, sig_syms)
            notify_outcomes(r, tf)
        except Exception:  # noqa
            log.error("avisos %s\n%s", r["symbol"], traceback.format_exc())

    secs = time.time() - t0
    forming = sum(1 for r in results if r["stage"] >= 3)
    log.info("[%s] Ciclo listo en %.0fs · analizados %d · errores %d · patrones activos %d · señales %d (enviadas %d, descartadas por coste %d)",
             tf, secs, len(results), len(errs), forming, len(sigs), sent, dropped_cost)
    send_radar(tf, results, sent, secs, len(syms), len(errs))


def monitor_loop():
    while True:
        try:
            trader.monitor()
        except Exception:  # noqa
            log.error("monitor\n%s", traceback.format_exc())
        time.sleep(config.MONITOR_SECS)


def tf_loop(tf, offset):
    tf_s = config.TF_SECONDS[tf]
    time.sleep(offset)
    first = config.SCAN_ON_START
    errors = 0
    while True:
        if first:
            first = False
        else:
            now = time.time()
            wake = (int(now // tf_s) + 1) * tf_s + config.CLOSE_DELAY_SECS
            time.sleep(max(1, wake - now))
        try:
            scan_cycle(tf)
            errors = 0
        except Exception as e:  # noqa
            errors += 1
            log.error("scan_cycle %s\n%s", tf, traceback.format_exc())
            if errors in (1, 5, 20):
                tg.send(f"⚠️ Error en el ciclo de escaneo {tf} ({errors}): {e}")


def main():
    mode = "📝 PAPEL (solo avisos)" if config.DRY_RUN else "⚡ REAL"
    if not config.DRY_RUN:
        if not (config.BINGX_API_KEY and config.BINGX_API_SECRET):
            log.error("Faltan BINGX_API_KEY / BINGX_API_SECRET")
            sys.exit(1)
        bx.set_hedge()
    tg.send(f"🤖 <b>Bot Quasimodo BingX iniciado</b>\nModo: {mode}\nTemporalidades: {', '.join(config.TIMEFRAMES)} · "
            f"riesgo {config.RISK_PCT}% · x{config.LEVERAGE} · máx. {config.MAX_OPEN} posiciones\n"
            f"Avisar con confluencia ≥ {params.min_score}/5 · operar con ≥ {config.TRADE_MIN_SCORE} · R:R mín. {params.min_rr} · "
            f"coste máx. {config.MAX_COST_R}R · entrada: {params.sig_mode} · HTF: {'sí' if config.USE_HTF else 'no'}\n"
            f"Radar cada {config.RADAR_EVERY_MIN} min · avisos de patrón: {','.join(sorted(config.NOTIFY_SETUPS)) or 'off'}")
    if not config.DRY_RUN:
        threading.Thread(target=monitor_loop, daemon=True).start()
    for k, tf in enumerate(config.TIMEFRAMES):
        threading.Thread(target=tf_loop, args=(tf, k * 15), daemon=True, name=f"scan-{tf}").start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
