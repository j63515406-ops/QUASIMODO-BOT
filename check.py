"""
Comprueba la robustez de backtest_trades.csv (generado por backtest.py).
Uso:  python check.py            (analiza todos los TF del csv)
      python check.py 4h a.csv b.csv   (varios csv juntos)
      python check.py 4h         (solo ese TF)
Muestra: intervalo de confianza del R medio, mitades temporales, largos/cortos, trimestres,
dependencia de pocas operaciones y de pocas monedas.
"""
import sys

import numpy as np
import pandas as pd


def stats(r):
    r = np.asarray(r, float)
    if len(r) == 0:
        return "sin datos"
    se = r.std(ddof=1) / np.sqrt(len(r)) if len(r) > 1 else float("nan")
    pos, neg = r[r > 0].sum(), -r[r < 0].sum()
    pf = pos / neg if neg > 0 else float("inf")
    return f"n={len(r):4d}  R medio {r.mean():+.2f} (±{1.96 * se:.2f})  PF {pf:.2f}  acierto {np.mean(r > 0) * 100:.0f}%"


def main():
    args = [x for x in sys.argv[1:] if not x.endswith(".csv")]
    files = [x for x in sys.argv[1:] if x.endswith(".csv")] or ["backtest_trades.csv"]
    df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    tfs = args or sorted(df["tf"].unique())
    rng = np.random.default_rng(1)
    for tf in tfs:
        d = df[df["tf"] == tf].sort_values("time").reset_index(drop=True)
        if d.empty:
            continue
        r = d["r"].values
        print(f"\n===== {tf} =====")
        print("Total            ", stats(r))
        boot = [rng.choice(r, len(r)).mean() for _ in range(5000)]
        print(f"Bootstrap 95%     R medio entre {np.percentile(boot, 2.5):+.2f} y {np.percentile(boot, 97.5):+.2f} "
              f"· prob. de R medio > 0: {np.mean(np.array(boot) > 0) * 100:.0f}%")
        h = len(d) // 2
        print("1ª mitad (antigua)", stats(r[:h]))
        print("2ª mitad (reciente)", stats(r[h:]))
        for i, part in enumerate(np.array_split(r, 4), 1):
            print(f"  cuarto {i}        ", stats(part))
        print("LONG             ", stats(d[d["dir"] == 1]["r"]))
        print("SHORT            ", stats(d[d["dir"] == -1]["r"]))
        srt = np.sort(r)[::-1]
        k = max(1, int(len(r) * 0.05))
        print(f"Sin el 5% mejor ({k} trades): R medio {srt[k:].mean():+.2f}  (total con todo {r.mean():+.2f})")
        by = d.groupby("symbol")["r"].sum().sort_values(ascending=False)
        print(f"Monedas distintas: {len(by)} · las 5 mejores aportan {by.head(5).sum():+.1f}R de {r.sum():+.1f}R: "
              + ", ".join(f"{s} {v:+.1f}" for s, v in by.head(5).items()))
        t = pd.to_datetime(d["time"], unit="ms")
        m = d.assign(m=t.dt.to_period("M").astype(str)).groupby("m")["r"].agg(["count", "sum"])
        print("R por mes:", " ".join(f"{i}:{row['sum']:+.1f}({int(row['count'])})" for i, row in m.iterrows()))


if __name__ == "__main__":
    main()
