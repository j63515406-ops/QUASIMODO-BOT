"""
Backtest de la estrategia QM por temporalidad con datos reales de BingX (endpoints publicos, sin claves).

Uso:   python backtest.py                      (compara 5m,15m,30m,1h,4h con las 40 monedas mas liquidas)
       python backtest.py --tfs 15m,1h --symbols 80 --candles 4000 --min-score 3 --mode confirm

Modelo de salida: 50% en TP1, SL a break-even tras TP1, resto en TP2; si SL y TP caen en la misma
vela se asume SL primero (conservador). Se descuentan comisiones + deslizamiento (--fee, ida y vuelta, en %).
Resultado en multiplos de R (1R = distancia entrada-SL). Guarda backtest_trades.csv.
"""
import argparse
import time

import numpy as np
import pandas as pd

import config
from bingx import BingX
from strategy import Params, run_qm


def fetch_history(bx, sym, tf, total):
    tf_ms = config.TF_SECONDS[tf] * 1000
    out, end = {}, None
    while len(out) < total:
        kl = bx.klines(sym, tf, min(1000, total - len(out) + 50), end_time=end)
        if not kl:
            break
        new = 0
        for k in kl:
            t = int(k["time"])
            if t not in out:
                out[t] = k
                new += 1
        if new == 0:
            break
        end = min(out) - 1
        if len(kl) < 50:
            break
    df = pd.DataFrame(sorted(out.values(), key=lambda k: int(k["time"])))
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "time"):
        df[c] = pd.to_numeric(df[c])
    df = df[df["time"] + tf_ms <= time.time() * 1000].reset_index(drop=True)
    return df.tail(total).reset_index(drop=True)


def simulate(df, sg, fee_pct=0.12, tp1_frac=0.5, move_be=True, max_bars=150):
    d, e, sl, t1, t2 = sg["dir"], sg["entry"], sg["sl"], sg["tp1"], sg["tp2"]
    risk = abs(e - sl)
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    unit = lambda px: (px - e) * d / risk  # noqa
    stop, rem, r, tp1_done, tp2_done, stopped = sl, 1.0, 0.0, False, False, False
    last = min(len(df) - 1, sg["bar"] + max_bars)
    if sg["bar"] >= len(df) - 1:
        return None  # sin velas posteriores
    done = False
    for j in range(sg["bar"] + 1, last + 1):
        hit_stop = l[j] <= stop if d == 1 else h[j] >= stop
        if hit_stop:
            r += rem * unit(stop)
            stopped = not tp1_done
            done = True
            break
        if not tp1_done and (h[j] >= t1 if d == 1 else l[j] <= t1):
            r += tp1_frac * unit(t1)
            rem -= tp1_frac
            tp1_done = True
            if move_be:
                stop = e
        if tp1_done and (h[j] >= t2 if d == 1 else l[j] <= t2):
            r += rem * unit(t2)
            tp2_done = True
            done = True
            break
    if not done:
        r += rem * unit(c[last])
    r -= fee_pct / 100.0 * e / risk
    return dict(r=r, tp1=tp1_done, tp2=tp2_done, sl=stopped, bars=j - sg["bar"] if done else last - sg["bar"],
                risk_pct=risk / e * 100)


def summarize(trades, days):
    if not trades:
        return None
    r = np.array([t["r"] for t in trades])
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    return dict(n=len(r), por_dia=len(r) / max(days, 1e-9), win=(r > 0).mean() * 100,
                tp1=np.mean([t["tp1"] for t in trades]) * 100, tp2=np.mean([t["tp2"] for t in trades]) * 100,
                sl=np.mean([t["sl"] for t in trades]) * 100, avgR=r.mean(), totR=r.sum(),
                pf=pos / neg if neg > 0 else float("inf"), risk=np.mean([t["risk_pct"] for t in trades]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tfs", default="5m,15m,30m,1h,4h")
    ap.add_argument("--symbols", type=int, default=40)
    ap.add_argument("--candles", type=int, default=3000)
    ap.add_argument("--min-score", type=int, default=config.STRATEGY["min_score"])
    ap.add_argument("--mode", default=config.STRATEGY["sig_mode"], choices=["confirm", "touch"])
    ap.add_argument("--fee", type=float, default=0.12, help="comision+deslizamiento ida y vuelta, en %%")
    a = ap.parse_args()

    bx = BingX()
    cs = bx.contracts()
    tk = {t["symbol"]: t for t in bx.tickers()}
    cand = []
    for c in cs:
        s = c.get("symbol", "")
        if not s.endswith("-USDT") or int(c.get("status", 1)) != 1 or any(s.startswith(p) for p in config.EXCLUDE_PREFIX):
            continue
        t = tk.get(s, {})
        try:
            qv = float(t.get("quoteVolume") or 0) or float(t.get("volume") or 0) * float(t.get("lastPrice") or 0)
        except Exception:  # noqa
            qv = 0
        cand.append((qv, s))
    syms = [s for _, s in sorted(cand, reverse=True)[:a.symbols]]
    print(f"{len(syms)} monedas · score>={a.min_score} · modo {a.mode} · coste {a.fee}% · {a.candles} velas por moneda\n")

    p = Params(**dict(config.STRATEGY, min_score=a.min_score, sig_mode=a.mode))
    rows, allt = [], []
    for tf in a.tfs.split(","):
        trades, spans = [], []
        for s in syms:
            try:
                df = fetch_history(bx, s, tf, a.candles)
                if len(df) < 300:
                    continue
                res = run_qm(df, p, collect=True)
                spans.append((df["time"].iloc[-1] - df["time"].iloc[0]) / 86_400_000)
                for sg in res.get("all_sigs", []):
                    o = simulate(df, sg, fee_pct=a.fee)
                    if o:
                        o.update(symbol=s, tf=tf, dir=sg["dir"], score=sg["score"], time=int(df["time"].iloc[sg["bar"]]))
                        trades.append(o)
            except Exception as e:  # noqa
                print(f"  {tf} {s}: {e}")
        days = float(np.mean(spans)) if spans else 0
        sm = summarize(trades, days * max(len(spans), 1))
        allt += trades
        if sm:
            rows.append((tf, sm, days, len(spans)))
        print(f"[{tf}] listo: {len(trades)} señales en {len(spans)} monedas (~{days:.0f} días)")

    print("\n TF   señales  /moneda·día  acierto  TP1%  TP2%  SL%   R medio  R total  P.Factor  riesgo%")
    for tf, m, days, ns in rows:
        print(f"{tf:>4} {m['n']:8d} {m['por_dia']:11.3f} {m['win']:7.0f}% {m['tp1']:5.0f} {m['tp2']:5.0f} {m['sl']:4.0f} "
              f"{m['avgR']:+8.2f} {m['totR']:+8.1f} {m['pf']:9.2f} {m['risk']:8.2f}")
    if allt:
        pd.DataFrame(allt).to_csv("backtest_trades.csv", index=False)
        print("\nDetalle guardado en backtest_trades.csv")
    print("\nMejor TF = mayor 'R medio' (esperanza) con suficientes señales (>100) y P.Factor > 1.2. "
          "Cuidado: pasado no garantiza futuro; valida con otro periodo/monedas.")


if __name__ == "__main__":
    main()
