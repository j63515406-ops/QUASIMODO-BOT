"""
Backtest del bot Quasimodo para ELEGIR TEMPORALIDAD con datos reales de BingX (solo endpoints publicos, no hacen falta claves).

  python backtest.py                                   # 5m,15m,30m,1h,4h · 40 monedas · 60 dias
  python backtest.py --tfs 15m,1h --symbols 60 --days 90 --scores 1,2,3,4

Misma logica que el bot en vivo (strategy.run_qm), entrada al cierre de la vela de la señal, SL/TP1/TP2/break-even,
comisiones y deslizamiento. Si SL y TP caen en la misma vela se asume que salta el SL (criterio conservador).
Muestra tambien las estadisticas del patron que da el HUD del Pine (QML alcanzado, T1, T2, cabeza rota).
"""
import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import config
from bingx import BingX, to_df
from strategy import Params, cost_r, run_qm


def pick_symbols(bx, k):
    cs = bx.contracts()
    tk = {t["symbol"]: t for t in bx.tickers()}
    rows = []
    for c in cs:
        s = c.get("symbol", "")
        if not s.endswith("-USDT") or int(c.get("status", 1)) != 1:
            continue
        if any(s.upper().startswith(p) for p in config.EXCLUDE_PREFIX):
            continue
        t = tk.get(s, {})
        try:
            qv = float(t.get("quoteVolume") or 0) or float(t.get("volume") or 0) * float(t.get("lastPrice") or 0)
        except Exception:  # noqa
            qv = 0
        if qv >= config.MIN_QUOTE_VOLUME:
            rows.append((qv, s))
    rows.sort(reverse=True)
    syms = [s for _, s in rows]
    if k >= len(syms):
        return syms
    step = len(syms) / k                      # muestra estratificada por volumen (no solo las mayores)
    return [syms[int(i * step)] for i in range(k)]


def simulate(sg, o, h, l, c, tp1_frac, be, fee_pct, slip_pct, max_bars):
    """Devuelve (R_bruto, R_neto, velas, resultado)."""
    i = sg["bar"]
    n = len(c)
    d = sg["dir"]
    entry, sl, tp1, tp2 = sg["entry"], sg["sl"], sg["tp1"], sg["tp2"]
    risk = abs(entry - sl)
    sl_cur = sl
    tp1_done = False
    realized = 0.0
    rem = 1.0
    end = min(n - 1, i + max_bars)
    res = "timeout"
    j = i
    for j in range(i + 1, end + 1):
        hit_sl = (l[j] <= sl_cur) if d == 1 else (h[j] >= sl_cur)
        if not tp1_done:
            hit_tp1 = (h[j] >= tp1) if d == 1 else (l[j] <= tp1)
            if hit_sl:
                realized += rem * d * (sl_cur - entry) / risk
                rem, res = 0.0, "SL"
                break
            if hit_tp1:
                realized += tp1_frac * d * (tp1 - entry) / risk
                rem -= tp1_frac
                tp1_done = True
                if be:
                    sl_cur = entry
                hit_tp2 = (h[j] >= tp2) if d == 1 else (l[j] <= tp2)
                if hit_tp2:
                    realized += rem * d * (tp2 - entry) / risk
                    rem, res = 0.0, "TP2"
                    break
        else:
            hit_tp2 = (h[j] >= tp2) if d == 1 else (l[j] <= tp2)
            if hit_sl:
                realized += rem * d * (sl_cur - entry) / risk
                rem, res = 0.0, "BE" if be else "SL"
                break
            if hit_tp2:
                realized += rem * d * (tp2 - entry) / risk
                rem, res = 0.0, "TP2"
                break
    if rem > 0:                                # tiempo agotado: cierre a mercado
        realized += rem * d * (c[j] - entry) / risk
        res = "timeout" if not tp1_done else "T1+timeout"
    net = realized - cost_r(entry, sl, fee_pct, slip_pct)
    return realized, net, j - i, res


def run(args):
    bx = BingX()
    tfs = [t.strip().lower() for t in args.tfs.split(",") if t.strip()]
    for t in tfs:
        if t not in config.TF_SECONDS:
            sys.exit(f"temporalidad no soportada: {t}")
    scores = [int(x) for x in args.scores.split(",")] if args.scores else [config.STRATEGY["min_score"]]
    syms = pick_symbols(bx, args.symbols)
    print(f"{len(syms)} monedas · {args.days} dias · TFs {tfs} · scores {scores} · fee {args.fee}%/lado + slip {args.slip}%/lado")

    # --- descarga (una vez por TF y moneda)
    data = {}
    jobs = [(tf, s) for tf in tfs for s in syms]

    def fetch(job):
        tf, s = job
        total = int(args.days * 86400 / config.TF_SECONDS[tf])
        try:
            df = to_df(bx.klines_hist(s, tf, total), config.TF_SECONDS[tf] * 1000)
            return job, df if len(df) >= 150 else None
        except Exception as e:  # noqa
            return job, None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=config.WORKERS) as ex:
        for job, df in ex.map(fetch, jobs):
            if df is not None:
                data[job] = df
    print(f"descargadas {len(data)}/{len(jobs)} series en {time.time() - t0:.0f}s")
    if not data:
        sys.exit("No se pudo descargar ninguna serie (revisa red / BINGX_BASE_URL).")

    base = dict(config.STRATEGY)
    rows, trades = [], []
    for tf in tfs:
        for sc in scores:
            p = Params(**dict(base, min_score=sc))
            pat = dict(formed=0, tap=0, t1=0, t2=0, fail=0)
            rs, rg, bars, costs, risks, res_cnt, ts = [], [], [], [], [], {}, []
            used, span_days = 0, 0.0
            tmin, tmax = None, None
            for s in syms:
                df = data.get((tf, s))
                if df is None:
                    continue
                used += 1
                t_a, t_b = int(df["time"].iloc[0]), int(df["time"].iloc[-1])
                tmin = t_a if tmin is None else min(tmin, t_a)
                tmax = t_b if tmax is None else max(tmax, t_b)
                span_days = max(span_days, len(df) * config.TF_SECONDS[tf] / 86400)
                r = run_qm(df, p)
                if not r:
                    continue
                for q in r["patterns"]:
                    pat["formed"] += 1
                    if q["tap"] is not None:
                        pat["tap"] += 1
                        pat["t1"] += q["hit"] >= 1
                        pat["t2"] += q["hit"] >= 2
                        pat["fail"] += q["result"] == "head_broken"
                o, h, l, c = (df[k].values for k in ("open", "high", "low", "close"))
                for sg in r["signals"]:
                    cr = cost_r(sg["entry"], sg["sl"], args.fee, args.slip)
                    if cr > args.max_cost_r:
                        continue
                    g, nt, nb, res = simulate(sg, o, h, l, c, config.TP1_PCT / 100.0, config.MOVE_BE,
                                              args.fee, args.slip, args.max_bars)
                    rg.append(g); rs.append(nt); bars.append(nb); costs.append(cr)
                    ts.append(int(df["time"].iloc[sg["bar"]]))
                    risks.append(abs(sg["entry"] - sg["sl"]) / sg["entry"] * 100)
                    res_cnt[res] = res_cnt.get(res, 0) + 1
                    trades.append([tf, sc, s, sg["dir"], sg["score"], sg["bar"], int(df["time"].iloc[sg["bar"]]),
                                   round(sg["entry"], 8), round(sg["sl"], 8), round(g, 3), round(nt, 3), nb, res])
            nsig = len(rs)
            se = float(np.std(rs, ddof=1) / np.sqrt(nsig)) if nsig > 1 else float("nan")
            mid = (tmin + tmax) / 2 if tmin is not None else 0
            a1 = [x for x, t in zip(rs, ts) if t < mid]
            a2 = [x for x, t in zip(rs, ts) if t >= mid]
            wins = sum(1 for x in rs if x > 0)
            gp = sum(x for x in rs if x > 0)
            gl = -sum(x for x in rs if x < 0)
            rows.append(dict(
                tf=tf, score=sc, coins=used, days=span_days, qm=pat["formed"], tap=pat["tap"], t1=pat["t1"], t2=pat["t2"],
                fail=pat["fail"], n=nsig, per_day=nsig / span_days if span_days else 0,
                win=100 * wins / nsig if nsig else float("nan"), avg_net=float(np.mean(rs)) if nsig else float("nan"),
                avg_gross=float(np.mean(rg)) if nsig else float("nan"), se=se,
                h1=float(np.mean(a1)) if a1 else float("nan"), n1=len(a1), h2=float(np.mean(a2)) if a2 else float("nan"), n2=len(a2),
                pf=gp / gl if gl > 0 else (float("inf") if gp > 0 else float("nan")),
                tot=float(np.sum(rs)) if nsig else 0.0, cost=float(np.mean(costs)) if nsig else float("nan"),
                risk=float(np.mean(risks)) if nsig else float("nan"), bars=float(np.mean(bars)) if nsig else float("nan"),
                res=res_cnt))

    pc = lambda a, b: f"{100 * a / b:.0f}%" if b else "—"  # noqa
    print("\n=== PATRON (equivalente al HUD del Pine) ===")
    print(f"{'TF':<5}{'monedas':>8}{'QM':>6}{'QML%':>7}{'T1%':>6}{'T2%':>6}{'cabeza rota%':>14}   (T1/T2/rota sobre los que tocaron QML)")
    seen = set()
    for r in rows:
        if r["tf"] in seen:
            continue
        seen.add(r["tf"])
        print(f"{r['tf']:<5}{r['coins']:>8}{r['qm']:>6}{pc(r['tap'], r['qm']):>7}{pc(r['t1'], r['tap']):>6}"
              f"{pc(r['t2'], r['tap']):>6}{pc(r['fail'], r['tap']):>14}")
    print("\n=== SEÑALES DE ENTRADA (con comisiones y slippage) ===")
    print(f"{'TF':<5}{'score≥':>7}{'señales':>8}{'/dia':>7}{'win%':>6}{'R bruto':>9}{'R neto':>8}{'±2·SE':>7}{'PF':>6}{'R total':>9}{'coste R':>9}{'riesgo%':>9}{'velas':>7}")
    for r in rows:
        print(f"{r['tf']:<5}{r['score']:>7}{r['n']:>8}{r['per_day']:>7.2f}{r['win']:>6.0f}{r['avg_gross']:>9.2f}{r['avg_net']:>8.2f}{2 * r['se']:>7.2f}"
              f"{r['pf']:>6.2f}{r['tot']:>9.1f}{r['cost']:>9.2f}{r['risk']:>9.2f}{r['bars']:>7.0f}")
    print("\n=== ESTABILIDAD: R neto en la 1ª mitad vs 2ª mitad del periodo ===")
    print(f"{'TF':<5}{'score≥':>7}{'1ª mitad':>12}{'n':>6}{'2ª mitad':>12}{'n':>6}   veredicto")
    for r in rows:
        if r["n"] < 2:
            verdict = "sin datos"
        elif r["avg_net"] - 2 * r["se"] > 0 and r["h1"] > 0 and r["h2"] > 0:
            verdict = "POSITIVO y estable"
        elif r["avg_net"] + 2 * r["se"] < 0:
            verdict = "NEGATIVO (descartar)"
        else:
            verdict = "no distinguible de 0"
        print(f"{r['tf']:<5}{r['score']:>7}{r['h1']:>12.2f}{r['n1']:>6}{r['h2']:>12.2f}{r['n2']:>6}   {verdict}")
    print("\nR neto = expectativa por señal tras costes; ±2·SE es su margen de error aproximado (~95%). Solo es fiable si el margen no cruza 0, "
          "las dos mitades del periodo coinciden y hay >=100 señales.")

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tf", "min_score", "symbol", "dir", "score", "bar", "bar_time", "entry", "sl", "R_bruto", "R_neto", "velas", "resultado"])
        w.writerows(trades)
    print(f"\nOperaciones guardadas en {args.out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", default="5m,15m,30m,1h,4h")
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--scores", default="", help="lista de min_score a comparar, ej. 1,2,3,4 (por defecto el de config)")
    ap.add_argument("--fee", type=float, default=config.FEE_PCT, help="comision taker por lado, %%")
    ap.add_argument("--slip", type=float, default=config.SLIP_PCT, help="slippage por lado, %%")
    ap.add_argument("--max-cost-r", type=float, default=config.MAX_COST_R)
    ap.add_argument("--max-bars", type=int, default=150, help="velas maximas en operacion antes de cerrar a mercado")
    ap.add_argument("--out", default="backtest_trades.csv")
    run(ap.parse_args())
