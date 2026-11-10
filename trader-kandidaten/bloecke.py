#!/usr/bin/env python3
"""Momentum-Blöcke: hold the strongest stocks for blocks of 25 trading days (about five weeks).

  bloecke.py tick [--from state.json] [--out state.json] [--asof YYYY-MM-DD] [--json]
  bloecke.py rangliste [--top 10]                 today's ranking, nothing changes

Three paper variants run side by side, each with its own 500 EUR:
  A  the one strongest stock
  B  the three strongest stocks, money split evenly
  C  the whole market (SPY), bought once and held: the yardstick

"Strongest" = the biggest rise over the last 126 trading days (about six months) among the
stocks in labor-aktien.txt that close above their 200-day average. A block is bought at a
close and sold at the close 25 trading days later; then the next block starts with the new
ranking. Fees as configured (fee_per_order_eur per buy and per sell), fractional shares,
dollars converted with eur_usd. There is no stop: a bad block can lose 20 % or more.

`tick` is meant to run once a day after the close. It values every variant, closes a block
that has run its 25 days, opens the next one and prints a line "NEUER BLOCK ..." when it did.
The state is a JSON object {"A": {...}, "B": {...}, "C": {...}}; an empty or missing file
starts all three at the given day's close.

A test in paper trading. Nothing here places an order. Not financial advice.
Standard library only, Python 3.8+.
"""

import argparse
import concurrent.futures
import datetime
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BLOCK_DAYS = 25
LOOKBACK = 126
TREND = 200
VARIANTS = {"A": {"name": "1 stärkste Aktie", "top": 1}, "B": {"name": "3 stärkste Aktien", "top": 3},
            "C": {"name": "Markt halten (SPY)", "top": 0}}
MARKET = "SPY"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


K = _load(os.path.join(HERE, "kandidaten.py"), "trader_kandidaten_for_bloecke")
S = _load(os.path.join(HERE, "swing.py"), "trader_swing_for_bloecke")
L = _load(os.path.join(HERE, "labor.py"), "trader_labor_for_bloecke")


class BlockError(Exception):
    pass


# ---------------------------------------------------------------- data and ranking

def fetch(syms, rng="2y"):
    def one(s):
        try:
            return s, S.daily_bars(s, rng)
        except Exception:
            return s, None
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        return {s: b for s, b in ex.map(one, syms) if b}


def upto(bars, day):
    """Bars up to and including `day`."""
    return [b for b in bars if b["d"] <= day]


def ranking(data, day):
    """[(rise, symbol, close)] strongest first, for stocks above their 200-day average on `day`."""
    out = []
    for s, bars in data.items():
        if s == MARKET:
            continue
        b = upto(bars, day)
        if len(b) < TREND + 1 or b[-1]["d"] != day:
            continue
        closes = [x["c"] for x in b]
        if closes[-1] <= sum(closes[-TREND:]) / TREND:
            continue
        rise = closes[-1] / closes[-1 - LOOKBACK] - 1
        out.append((rise, s, closes[-1]))
    out.sort(key=lambda x: (-x[0], x[1]))
    return out


def close_on(data, sym, day):
    b = upto(data.get(sym) or [], day)
    return b[-1]["c"] if b else None


def trading_days_between(data, start, day):
    """Sessions after `start` up to and including `day`, counted on the market's calendar."""
    return sum(1 for b in data.get(MARKET) or [] if start < b["d"] <= day)


def planned_end(data, start, n=BLOCK_DAYS):
    """The day a block that started on `start` ends: n sessions later (estimated with weekdays
    past the last known session)."""
    days = [b["d"] for b in data.get(MARKET) or [] if b["d"] > start]
    if len(days) >= n:
        return days[n - 1]
    d = days[-1] if days else start
    left = n - len(days)
    while left > 0:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


# ---------------------------------------------------------------- the paper accounts

def new_variant(key, cfg):
    v = VARIANTS[key]
    return {"variante": key, "name": v["name"], "top": v["top"], "start_eur": cfg["budget_eur"],
            "bar_eur": cfg["budget_eur"], "positionen": [], "block_nr": 0, "block_start": None,
            "block_ende": None, "wert_eur": cfg["budget_eur"], "gebuehren_eur": 0.0, "verlauf": [], "bloecke": []}


def value(v, data, day, fx):
    return v["bar_eur"] + sum(p["stueck"] * (close_on(data, p["ticker"], day) or p["kauf_usd"]) / fx
                              for p in v["positionen"])


def sell_all(v, data, day, cfg):
    fx, fee = cfg["eur_usd"], cfg["fee_per_order_eur"]
    picks = []
    for p in v["positionen"]:
        px = close_on(data, p["ticker"], day) or p["kauf_usd"]
        v["bar_eur"] += p["stueck"] * px / fx - fee
        v["gebuehren_eur"] += fee
        picks.append({"ticker": p["ticker"], "kauf_usd": p["kauf_usd"], "verkauf_usd": round(px, 2),
                      "pct": round((px / p["kauf_usd"] - 1) * 100, 2)})
    v["positionen"] = []
    return picks


def buy(v, syms, data, day, cfg):
    fx, fee = cfg["eur_usd"], cfg["fee_per_order_eur"]
    if not syms:
        return
    each = (v["bar_eur"] - fee * len(syms)) / len(syms)
    for s in syms:
        px = close_on(data, s, day)
        if not px or each <= 0:
            continue
        stueck = round(each * fx / px, 3) if cfg.get("fractional", True) else float(int(each * fx / px))
        if stueck <= 0:
            continue
        v["bar_eur"] -= stueck * px / fx + fee
        v["gebuehren_eur"] += fee
        v["positionen"].append({"ticker": s, "kauf_usd": round(px, 2), "stueck": stueck,
                                "einsatz_eur": round(stueck * px / fx, 2), "kauf_datum": day.isoformat()})


def tick(state, data, day, cfg):
    """Advance all variants to `day`'s close. Returns (state, news lines)."""
    fx = cfg["eur_usd"]
    news = []
    rank = None
    for key in ("A", "B", "C"):
        v = state.get(key) or new_variant(key, cfg)
        state[key] = v
        if key == "C":
            if not v["positionen"]:
                buy(v, [MARKET], data, day, cfg)
                v["block_nr"], v["block_start"] = 1, day.isoformat()
                news.append("NEUER BLOCK C: Markt (SPY) gekauft, wird gehalten.")
        else:
            start = datetime.date.fromisoformat(v["block_start"]) if v["block_start"] else None
            due = start is None or trading_days_between(data, start, day) >= BLOCK_DAYS
            if due:
                if v["positionen"]:
                    before = v["wert_eur_block_start"]
                    picks = sell_all(v, data, day, cfg)
                    after = v["bar_eur"]
                    v["bloecke"].append({"nr": v["block_nr"], "start": v["block_start"], "ende": day.isoformat(),
                                         "aktien": picks, "ergebnis_eur": round(after - before, 2),
                                         "ergebnis_pct": round((after / before - 1) * 100, 2)})
                if rank is None:
                    rank = ranking(data, day)
                syms = [s for _, s, _ in rank[:v["top"]]]
                v["wert_eur_block_start"] = round(v["bar_eur"], 2)
                buy(v, syms, data, day, cfg)
                v["block_nr"] += 1
                v["block_start"] = day.isoformat()
                v["block_ende"] = planned_end(data, day).isoformat()
                news.append("NEUER BLOCK %s (%s): %s, bis etwa %s." % (
                    key, v["name"], ", ".join("%s %s Stk. zu %s $" % (p["ticker"], K.shares_text(p["stueck"]),
                                                                     K.de(p["kauf_usd"])) for p in v["positionen"]),
                    datetime.date.fromisoformat(v["block_ende"]).strftime("%d.%m")))
        v["wert_eur"] = round(value(v, data, day, fx), 2)
        v["rendite_pct"] = round((v["wert_eur"] / v["start_eur"] - 1) * 100, 2)
        v["stand"] = day.isoformat()
        if v["block_start"] and key != "C":
            v["tage_im_block"] = trading_days_between(data, datetime.date.fromisoformat(v["block_start"]), day)
        for p in v["positionen"]:
            px = close_on(data, p["ticker"], day)
            if px:
                p["kurs_usd"] = round(px, 2)
                p["pct"] = round((px / p["kauf_usd"] - 1) * 100, 2)
        if not v["verlauf"] or v["verlauf"][-1][0] != day.isoformat():
            v["verlauf"].append([day.isoformat(), v["wert_eur"]])
        else:
            v["verlauf"][-1][1] = v["wert_eur"]
    return state, news


def last_session(data):
    m = data.get(MARKET) or []
    if not m:
        raise BlockError("keine Kurse für %s" % MARKET)
    d = m[-1]["d"]
    now = E_now()
    if d == now.date() and not S.session_over(d):
        d = m[-2]["d"]
    return d


def E_now():
    return S.E.to_eastern(S.E.utc_now())


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="trader/kandidaten.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tick", help="alle Varianten bis zum letzten Schlusskurs fortschreiben")
    p.add_argument("--from", dest="from_file")
    p.add_argument("--out")
    p.add_argument("--asof", help="so tun, als wäre dieser Tag der letzte Handelstag (YYYY-MM-DD)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("rangliste", help="die heutige Rangliste zeigen, ohne etwas zu ändern")
    p.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    try:
        cfg = K.read_config(a.config)
        syms = L.symbols() + [MARKET]
        data = fetch(syms)
        day = datetime.date.fromisoformat(a.asof) if getattr(a, "asof", None) else last_session(data)
        if a.cmd == "rangliste":
            for i, (rise, s, c) in enumerate(ranking(data, day)[:a.top], 1):
                print("%2d. %-6s %+7.1f %% in 6 Monaten, Schluss %s $" % (i, s, rise * 100, K.de(c)))
            return 0
        state = {}
        if a.from_file and os.path.exists(a.from_file):
            with open(a.from_file, encoding="utf-8") as fh:
                state = json.load(fh) or {}
        state, news = tick(state, data, day, cfg)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
        if a.json:
            print(json.dumps(state, ensure_ascii=False, indent=1))
            return 0
        print("Momentum-Blöcke, Stand %s (Schlusskurse). Je 500 € Startkapital, Gebühren wie im Tool." % day.strftime("%d.%m.%Y"))
        for key in ("A", "B", "C"):
            v = state[key]
            print("%s %-20s %s €  (%+.1f %%)  %s" % (key, v["name"], K.de(v["wert_eur"]), v["rendite_pct"],
                  ", ".join("%s %+.1f %%" % (p["ticker"], p.get("pct", 0)) for p in v["positionen"])))
        for n in news:
            print(n)
        print("Ein Block läuft %d Handelstage. Kein Stop: ein schlechter Block kann 20 %% und mehr kosten. %s" % (
            BLOCK_DAYS, K.NOTE))
        return 0
    except (BlockError, K.KError) as e:
        sys.stderr.write("bloecke.py: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
