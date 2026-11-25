import numpy as np
import pandas as pd


def rma(s, n):
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def atr(h, l, c, n):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def rsi(c, n):
    d = c.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    ru, rd = rma(up, n), rma(dn, n)
    rs = ru / rd.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(~((rd == 0) & (ru > 0)), 100.0)


def supertrend(h, l, c, length, factor):
    """Replica del Supertrend propio del Pine. dir<0 = tendencia alcista."""
    a = atr(pd.Series(h), pd.Series(l), pd.Series(c), length).values
    n = len(c)
    f_up = np.full(n, np.nan)
    f_lo = np.full(n, np.nan)
    line = np.full(n, np.nan)
    d = np.ones(n, dtype=int)
    for i in range(n):
        if np.isnan(a[i]):
            continue
        hl2 = (h[i] + l[i]) / 2
        up, lo = hl2 + factor * a[i], hl2 - factor * a[i]
        pu = f_up[i - 1] if i > 0 else np.nan
        pl_ = f_lo[i - 1] if i > 0 else np.nan
        f_up[i] = up if (np.isnan(pu) or up < pu or c[i - 1] > pu) else pu
        f_lo[i] = lo if (np.isnan(pl_) or lo > pl_ or c[i - 1] < pl_) else pl_
        pline = line[i - 1] if i > 0 else np.nan
        if np.isnan(pline):
            d[i] = 1
        elif pline == pu:
            d[i] = -1 if c[i] > f_up[i] else 1
        else:
            d[i] = 1 if c[i] < f_lo[i] else -1
        line[i] = f_lo[i] if d[i] == -1 else f_up[i]
    return d, line


def pivots(high, low, L):
    """ph[b]/pl[b]: valor del pivote confirmado en la vela b (pivote en b-L)."""
    n = len(high)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for b in range(2 * L, n):
        c = b - L
        if high[c] >= high[b - 2 * L:c].max() and high[c] > high[c + 1:b + 1].max():
            ph[b] = high[c]
        if low[c] <= low[b - 2 * L:c].min() and low[c] < low[c + 1:b + 1].min():
            pl[b] = low[c]
    return ph, pl
