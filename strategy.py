"""
Port a Python del indicador Quasimodo (QML / BOS / barrida de liquidez).
Se reproduce la maquina de estados sobre las ultimas N velas CERRADAS y se
devuelven los eventos de la ultima vela. No repinta: solo velas cerradas.
Sin OI/liquidaciones (datos exclusivos de AlertaChart Pro): maximo 5 puntos.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

import indicators as ind


@dataclass
class Params:
    piv_len: int = 5
    min_sweep: float = 0.15
    min_leg: float = 1.5
    zone_atr: float = 0.35
    max_wait: int = 80
    max_run: int = 150
    sig_mode: str = "confirm"
    conf_win: int = 5
    sl_buf: float = 0.1
    min_score: int = 3
    st_len: int = 10
    st_fac: float = 3.0
    rsi_len: int = 14
    rsi_hi: int = 70
    rsi_lo: int = 30
    cvd_len: int = 3
    vol_mult: float = 1.2


def run_qm(df, p, htf_up=True, use_htf=False, collect=False):
    o = df["open"].values
    h = df["high"].values
    l = df["low"].values
    c = df["close"].values
    v = df["volume"].values
    n = len(df)
    L = p.piv_len

    sh, sl_, sc_ = pd.Series(h), pd.Series(l), pd.Series(c)
    atr14 = ind.atr(sh, sl_, sc_, 14).values
    rsi = ind.rsi(sc_, p.rsi_len).values
    vol_avg = pd.Series(v).rolling(20).mean().values
    rng = h - l
    delta = np.where(rng > 0, v * ((c - l) - (h - c)) / np.where(rng > 0, rng, 1), 0.0)
    cvd = np.cumsum(delta)
    st_dir, _ = ind.supertrend(h, l, c, p.st_len, p.st_fac)
    ph, pl = ind.pivots(h, l, L)

    swings = []  # dicts: t(1/-1), p, b, r, c
    stage = 0
    d = 0
    lsP = midP = hdP = ext = zFar = t1 = t2 = slP = np.nan
    bosB = tapB = touchB = used_head = None
    hit_max = 0
    sig_done = True
    div_ok = div_r = div_c = False
    res = {}
    all_sigs = []

    start = max(60, 2 * L + 2)
    for i in range(start, n):
        a = atr14[i]
        ev = dict(bos=False, tap=False, hit=0, fail=False, end=False, sig=None, block=None, score=None)

        # --- swings confirmados en esta vela
        pb = i - L
        hv, lv = ph[i], pl[i]
        new = None
        if not np.isnan(hv) and not np.isnan(lv):
            new = (1, hv) if abs(hv - c[pb]) > abs(c[pb] - lv) else (-1, lv)
        elif not np.isnan(hv):
            new = (1, hv)
        elif not np.isnan(lv):
            new = (-1, lv)
        if new is not None:
            t, pv = new
            rec = dict(t=t, p=pv, b=pb, r=rsi[pb], c=cvd[pb])
            if swings and swings[-1]["t"] == t:
                if (pv > swings[-1]["p"]) if t == 1 else (pv < swings[-1]["p"]):
                    swings[-1] = rec
            else:
                swings.append(rec)
                if len(swings) > 20:
                    swings.pop(0)

        if np.isnan(a):
            continue

        # --- 4 · QML tocado: objetivos o fallo
        if stage == 4:
            fail_now = (c[i] > hdP) if d == -1 else (c[i] < hdP)
            if fail_now:
                ev["fail"] = True
            else:
                for k in (1, 2):
                    tk = t1 if k == 1 else t2
                    if hit_max == k - 1 and ((l[i] <= tk) if d == -1 else (h[i] >= tk)):
                        hit_max = k
                        ev["hit"] = k
            if ev["fail"] or hit_max == 2 or (i - tapB > p.max_run):
                ev["end"] = True
                stage, d = 0, 0

        # --- 3 · QM formado: esperar toque
        if stage == 3:
            ext = min(ext, l[i]) if d == -1 else max(ext, h[i])
            head_broken = (c[i] > hdP) if d == -1 else (c[i] < hdP)
            tapped = (h[i] >= zFar) if d == -1 else (l[i] <= zFar)
            if tapped and not head_broken:
                stage = 4
                ev["tap"] = True
                tapB = touchB = i
                hit_max = 0
                sig_done = False
                t1 = ext
                t2 = lsP - (hdP - ext)
                slP = hdP + p.sl_buf * a if d == -1 else hdP - p.sl_buf * a
            elif head_broken or (i - bosB > p.max_wait):
                ev["end"] = True
                stage, d = 0, 0

        # --- señal
        if stage == 4 and ((l[i] <= zFar) if d == 1 else (h[i] >= zFar)):
            touchB = i
        if stage == 4 and not sig_done and not ev["end"]:
            in_win = i - touchB <= p.conf_win
            confirm = (c[i] > o[i] and c[i] > zFar) if d == 1 else (c[i] < o[i] and c[i] < zFar)
            trigger = (i == tapB) if p.sig_mode == "touch" else (in_win and confirm)
            dir_ok = (t1 > c[i]) if d == 1 else (t1 < c[i])
            ok_st = st_dir[i] < 0 if d == 1 else st_dir[i] > 0
            ok_rsi = (rsi[i] > rsi[i - 1] and rsi[i] < p.rsi_hi) if d == 1 else (rsi[i] < rsi[i - 1] and rsi[i] > p.rsi_lo)
            ok_cvd = (cvd[i] > cvd[i - p.cvd_len]) if d == 1 else (cvd[i] < cvd[i - p.cvd_len])
            rvol = v[i] / vol_avg[i] if vol_avg[i] and vol_avg[i] > 0 else np.nan
            ok_vol = (not np.isnan(rvol)) and rvol >= p.vol_mult
            ok_htf = (not use_htf) or (htf_up if d == 1 else not htf_up)
            score = sum([ok_st, ok_rsi, div_ok, ok_cvd, ok_vol])
            ev["score"] = int(score)
            if hit_max < 1:
                if not trigger:
                    ev["block"] = "sin vela de confirmación" if in_win else "ventana de confirmación vencida"
                elif not ok_htf:
                    ev["block"] = "filtro HTF en contra"
                elif not dir_ok:
                    ev["block"] = "TP1 ya superado"
                elif score < p.min_score:
                    ev["block"] = f"confluencia {score}/{p.min_score}"
            if hit_max >= 1:
                sig_done = True
            elif trigger and ok_htf and dir_ok and score >= p.min_score and abs(c[i] - slP) > 0:
                sig_done = True
                risk = abs(c[i] - slP)
                ev["sig"] = dict(
                    dir=d, entry=float(c[i]), sl=float(slP), tp1=float(t1), tp2=float(t2),
                    rr1=abs(t1 - c[i]) / risk, rr2=abs(t2 - c[i]) / risk, score=int(score), max_score=5,
                    st=bool(ok_st), rsi_ok=bool(ok_rsi), div=bool(div_ok), div_rsi=bool(div_r), div_cvd=bool(div_c),
                    cvd=bool(ok_cvd), vol=bool(ok_vol), rvol=None if np.isnan(rvol) else float(rvol),
                    rsi=float(rsi[i]), qml=float(lsP), head=float(hdP), bos=float(midP), zfar=float(zFar),
                    atr=float(a), bar=i, mode=p.sig_mode)
                if collect:
                    all_sigs.append(dict(ev["sig"]))

        # --- 0 · buscando hombro -> swing medio -> cabeza + BOS
        if stage == 0 and len(swings) >= 3:
            A, M, H = swings[-3], swings[-2], swings[-1]
            fresh = used_head is None or H["b"] != used_head
            big = abs(M["p"] - H["p"]) >= p.min_leg * a
            bear = A["t"] == 1 and M["p"] < A["p"] and H["p"] > A["p"] + p.min_sweep * a and c[i] < M["p"] and big
            bull = A["t"] == -1 and M["p"] > A["p"] and H["p"] < A["p"] - p.min_sweep * a and c[i] > M["p"] and big
            if fresh and (bear or bull):
                stage = 3
                d = -1 if bear else 1
                ev["bos"] = True
                used_head = H["b"]
                div_r = bool(H["r"] < A["r"]) if bear else bool(H["r"] > A["r"])
                div_c = bool(H["c"] < A["c"]) if bear else bool(H["c"] > A["c"])
                div_ok = div_r or div_c
                lsP, midP, hdP = A["p"], M["p"], H["p"]
                ext = l[i] if bear else h[i]
                bosB = i
                zFar = lsP - p.zone_atr * a if bear else lsP + p.zone_atr * a

        res = dict(ev=ev, stage=stage, dir=d, qml=lsP, head=hdP, bos=midP, zfar=zFar,
                   atr=a, close=c[i], tp1=t1, tp2=t2, sl=slP, div_rsi=div_r, div_cvd=div_c,
                   sig_done=sig_done, hit_max=hit_max)
    if res:
        res["all_sigs"] = all_sigs
    return res
