#!/usr/bin/env python3
"""Regel-Check: does the rule make money right now, across many stocks, after fees?

  labor.py orb   [--days 60] [--delay 2] [--json] [--out FILE]
  labor.py swing [--range 2y] [--json] [--out FILE]
  labor.py alle  [--json] [--out FILE]          both, one JSON document

ORB: every stock in labor-aktien.txt, every session of the last 60 days (Yahoo keeps 60 days
of 5-minute bars). The live rule: 15-minute range, the first 5-minute close above its high up
to 11:00, a target at 2R at least min_target_pct away, stop at the range middle, flat 15:55.
The entry is the close `--delay` bars later (2 = ten minutes: the time a scan and a push need),
and only if the price is still above the range high then. Sized like the live tool (500 EUR,
2 % risk, fractional), fees of the live tool.

Swing: the live swing rule (swing.py) on two years of daily bars of the same stocks.

The verdict a scan uses ("ok"): the average result per trade after fees is above zero and the
sample is big enough. A single stock's own history is too small to tell skill from luck; the
rule across many stocks is not.

For a paper-trading test only. Nothing here places an order. Not financial advice.
Standard library only, Python 3.8+.
"""

import argparse
import concurrent.futures
import datetime
import importlib.util
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIST = os.path.join(HERE, "labor-aktien.txt")
OR_BARS = 3            # 15 minutes
LAST_SIGNAL_BAR = 17   # the bar 10:55-11:00 is the last a scan (until 11:01) can see
FLAT_BAR = 76          # 15:50-15:55 is the last bar held
MIN_N = {"orb": 300, "swing": 150}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


K = _load(os.path.join(HERE, "kandidaten.py"), "trader_kandidaten_for_labor")
E = K.entry_mod()


def symbols(path=LIST):
    with open(path, encoding="utf-8") as fh:
        return [ln.strip().upper() for ln in fh if ln.strip() and not ln.startswith("#")]


def fetch_all(syms, rng, interval, workers=8):
    def one(s):
        try:
            return s, E.parse_chart(E.yahoo_get(E.chart_path(s, rng, interval)), s)["bars"]
        except Exception:
            return s, None
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, syms))


# ---------------------------------------------------------------- ORB

def sessions(bars):
    """5-minute bars -> [(date, {slot: bar})], slot 0 = 9:30, complete-looking sessions only."""
    days = {}
    for b in bars:
        t = E.to_eastern(E.utc_of(b["t"]))
        m = t.hour * 60 + t.minute
        if 570 <= m < 960:
            days.setdefault(t.date(), {})[(m - 570) // 5] = b
    return [(d, D) for d, D in sorted(days.items()) if len(D) >= 70 and all(i in D for i in range(OR_BARS))]


def orb_trade(D, cfg, delay=2):
    """The live rule on one session. -> dict or None (no signal, not tradable)."""
    oh = max(D[i]["h"] for i in range(OR_BARS))
    ol = min(D[i]["l"] for i in range(OR_BARS))
    k = next((i for i in range(OR_BARS, LAST_SIGNAL_BAR + 1) if i in D and D[i]["c"] > oh), None)
    if k is None:
        return None
    ke = k + delay
    if ke not in D or ke >= FLAT_BAR:
        return None
    entry = D[ke]["c"]
    if entry <= oh:
        return {"skip": "unter Range-Hoch"}
    stop = (oh + ol) / 2
    rps = entry - stop
    if rps <= 0:
        return None
    target = entry + 2 * rps
    if (target / entry - 1) * 100 < cfg["min_target_pct"]:
        return {"skip": "Ziel zu nah"}
    ex = None
    for i in range(ke + 1, FLAT_BAR):
        if i not in D:
            continue
        if D[i]["l"] <= stop:
            ex = stop
            break
        if D[i]["h"] >= target:
            ex = target
            break
    if ex is None:
        ex = D[max(i for i in D if i < FLAT_BAR)]["c"]
    s = K.size_trade(entry, stop, target, cfg)
    shares = s.get("shares") or 0
    if not shares:
        return None
    fx = cfg["eur_usd"]
    gross = shares * (ex - entry) / fx
    return {"gross": gross, "net": gross - s["fees_eur"], "r": (gross - s["fees_eur"]) / s["risk_eur"]}


def summarize(trades, min_n, recent=None):
    n = len(trades)
    if not n:
        return {"n": 0, "ok": False, "grund": "keine Fälle"}
    net = [t["net"] for t in trades]
    out = {"n": n, "eur_pro_trade": round(statistics.mean(net), 2),
           "eur_pro_trade_ohne_gebuehren": round(statistics.mean(t["gross"] for t in trades), 2),
           "summe_eur": round(sum(net), 1), "gewinner_pct": round(sum(1 for x in net if x > 0) / n * 100),
           "r_pro_trade": round(statistics.mean(t["r"] for t in trades), 3)}
    if recent:
        rn = [t["net"] for t in recent]
        out["letzte_20_tage"] = {"n": len(rn), "eur_pro_trade": round(statistics.mean(rn), 2) if rn else None}
    out["ok"] = n >= min_n and out["eur_pro_trade"] > 0
    out["grund"] = ("die Regel verdient nach Gebühren" if out["ok"] else
                    "zu wenige Fälle" if n < min_n else "die Regel verliert nach Gebühren")
    return out


def run_orb(cfg, syms=None, delay=2, data=None):
    syms = syms or symbols()
    data = data or fetch_all(syms, "60d", "5m")
    trades, skipped, used = [], {}, 0
    dates = set()
    per_day = []
    for s, bars in data.items():
        if not bars:
            continue
        used += 1
        for d, D in sessions(bars):
            t = orb_trade(D, cfg, delay)
            if not t:
                continue
            if "skip" in t:
                skipped[t["skip"]] = skipped.get(t["skip"], 0) + 1
                continue
            t["d"] = d
            trades.append(t)
            dates.add(d)
    last20 = set(sorted(dates)[-20:])
    out = summarize(trades, MIN_N["orb"], [t for t in trades if t["d"] in last20])
    out.update({"aktien": used, "tage": len(dates), "verzoegerung_min": delay * 5, "ausgelassen": skipped,
                "regel": "15-Min-Range, Ausbruch bis 11:00, Einstieg %d Min. später, Stop Range-Mitte, Ziel 2R "
                         "(mind. %s %%), Schluss 15:55" % (delay * 5, K.de(cfg["min_target_pct"], 1))})
    return out


# ---------------------------------------------------------------- swing

def run_swing(cfg, syms=None, rng="2y", data=None):
    S = _load(os.path.join(HERE, "swing.py"), "trader_swing_for_labor")
    syms = syms or symbols()
    if data is None:
        def one(s):
            try:
                return s, S.daily_bars(s, rng)
            except Exception:
                return s, None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            data = dict(ex.map(one, syms))
    trades, used, cut = [], 0, None
    for s, bars in data.items():
        if not bars or len(bars) < 60:
            continue
        used += 1
        bt = S.backtest(bars, cfg)
        for t in bt["trades"]:
            risk = cfg["budget_eur"] * cfg["max_risk_pct"] / 100.0
            net = t["r_net"] * risk
            trades.append({"net": net, "gross": t["r"] * risk, "r": t["r_net"], "d": t["day"]})
    days = sorted({t["d"] for t in trades})
    cut = days[-20] if len(days) >= 20 else None
    out = summarize(trades, MIN_N["swing"], [t for t in trades if cut and t["d"] >= cut])
    out.update({"aktien": used, "regel": "Schluss über den 20 Tagen davor, Kauf über dem Hoch am nächsten Tag, "
                                         "Stop 1,5 ATR, Ziel 2R, höchstens 10 Tage"})
    return out


# ---------------------------------------------------------------- output

def text(doc):
    lines = ["Regel-Check %s (500 €-Rechnung, Gebühren und 2 %% Risiko wie im Tool)" % doc["stand"]]
    for key, name in (("orb", "Tages-Ausbruch"), ("swing", "Swing")):
        r = doc.get(key)
        if not r:
            continue
        if not r.get("n"):
            lines.append("%s: keine Fälle." % name)
            continue
        lines.append("%s: %s. %d Fälle, %s € pro Trade nach Gebühren (%s € ohne), %d %% Gewinner%s." % (
            name, "OK" if r["ok"] else "NEIN, " + r["grund"], r["n"], K.de(r["eur_pro_trade"]),
            K.de(r["eur_pro_trade_ohne_gebuehren"]), r["gewinner_pct"],
            ("; letzte 20 Tage %s €" % K.de(r["letzte_20_tage"]["eur_pro_trade"]))
            if r.get("letzte_20_tage", {}).get("eur_pro_trade") is not None else ""))
    lines.append("Nur wenn eine Regel hier OK ist, kann sie einen Kandidaten liefern. Not financial advice.")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    for name in ("orb", "swing", "alle"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument("--out")
        p.add_argument("--delay", type=int, default=2)
        p.add_argument("--range", default="2y")
        p.add_argument("--config")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 2
    cfg = K.read_config(a.config or "trader/kandidaten.json")
    doc = {"stand": E.to_eastern(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)).strftime("%Y-%m-%d %H:%M ET")}
    if a.cmd in ("orb", "alle"):
        doc["orb"] = run_orb(cfg, delay=a.delay)
    if a.cmd in ("swing", "alle"):
        doc["swing"] = run_swing(cfg, rng=a.range)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1)
    print(json.dumps(doc, ensure_ascii=False, indent=1) if a.json else text(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
