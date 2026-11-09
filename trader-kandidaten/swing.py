#!/usr/bin/env python3
"""Swing-Check (trader-kandidaten): a breakout held for days, tested on two years
of daily bars, for the same small account and the same fee hurdle as kandidaten.py.

The rule (one setup, kept simple so it can be tested honestly):

  signal   the day's close is above every close of the 20 days before it
           (the scanner's 20-day closing-high breakout)
  entry    the next trading day, once the price trades above the signal day's
           high: buy at that level, or at the open if it opens above it.
           No trade that day means no trade at all.
  stop     signal-day high minus 1.5 x ATR(14)   (the average daily range)
  target   2R above the planned entry
  exit     target, stop, or the close of the 10th trading day, whichever comes
           first. A gap through the stop at the open fills at the open (a real
           risk of holding overnight). Stop and target on the same day count as
           the stop.
  no trade when the company reported earnings in the last 2 days, a report is
           likely inside the holding window (last 10-Q/10-K + 91 days, +-10),
           or a headline says one is coming.

  swing.py scan [--symbols A,B]   tonight's signals: plan, backtest, verdict
  swing.py update --from open.json --out done.json
                                  advance open swing trades (journal documents)

Verdict "Swing-Kandidat": at least 8 past trades in the backtest, and their
average R, after each trade's own fees, at least the fee buffer (0.10 R).
For a paper-trading test only. Nothing here places an order.
Not financial advice. Standard library only, Python 3.8+.
"""

import argparse
import datetime
import importlib.util
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

HOLD_DAYS = 10
ATR_DAYS = 14
STOP_ATR = 1.5
TARGET_R = 2.0
LOOKBACK = 20
MIN_TRADES = 8
REPORT_EVERY = 91          # days between quarterly reports
REPORT_SLACK = 10          # days either side


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


K = _load(os.path.join(HERE, "kandidaten.py"), "trader_kandidaten_for_swing")
E = K.entry_mod()


class SwingError(Exception):
    pass


# ---------------------------------------------------------------- data

def daily_bars(sym, rng="2y", text=None):
    """[{d: date, o, h, l, c}] oldest first, complete sessions only (today's too after the close)."""
    if text is None:
        text = E.yahoo_get(E.chart_path(sym, rng, "1d"))
    ch = E.parse_chart(text, sym)
    out = []
    for b in ch["bars"]:
        d = E.to_eastern(E.utc_of(b["t"])).date()
        if out and out[-1]["d"] == d:
            out[-1] = {"d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        else:
            out.append({"d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return out


def atr(bars, i, n=ATR_DAYS):
    """Average true range of the n days ending at bar i."""
    if i < n:
        return None
    trs = []
    for j in range(i - n + 1, i + 1):
        pc = bars[j - 1]["c"]
        trs.append(max(bars[j]["h"] - bars[j]["l"], abs(bars[j]["h"] - pc), abs(bars[j]["l"] - pc)))
    return sum(trs) / n


def is_signal(bars, i):
    return i >= LOOKBACK and bars[i]["c"] > max(b["c"] for b in bars[i - LOOKBACK:i])


def plan_from(bars, i):
    """Levels for a signal on bar i: buy above its high, stop 1.5 ATR under, target 2R."""
    a = atr(bars, i)
    if not a:
        return None
    level = round(bars[i]["h"] + 0.01, 2)
    stop = math.floor((level - STOP_ATR * a) * 100) / 100.0
    target = round(level + TARGET_R * (level - stop), 2)
    return {"level": level, "stop": stop, "target": target, "atr": round(a, 4)}


def walk(bars, start, level, stop, target, hold=HOLD_DAYS):
    """From bar `start` (the day after the signal). Returns a dict with entered, entry,
    result, exit, exit_day, days, done (False while the 10 days are not over)."""
    if start >= len(bars):
        return {"entered": None, "done": False}
    d0 = bars[start]
    if d0["h"] < level:
        return {"entered": False, "done": True, "result": "nicht ausgelöst"}
    entry = max(d0["o"], level)
    for k in range(hold):
        j = start + k
        if j >= len(bars):
            return {"entered": True, "entry": entry, "entry_day": d0["d"], "done": False, "days": k}
        b = bars[j]
        if k > 0 and b["o"] <= stop:
            return dict(entered=True, entry=entry, entry_day=d0["d"], done=True, result="Stop (Lücke)",
                        exit=b["o"], exit_day=b["d"], days=k + 1)
        if k > 0 and b["o"] >= target:
            return dict(entered=True, entry=entry, entry_day=d0["d"], done=True, result="Ziel (Lücke)",
                        exit=b["o"], exit_day=b["d"], days=k + 1)
        if b["l"] <= stop:
            return dict(entered=True, entry=entry, entry_day=d0["d"], done=True, result="Stop",
                        exit=stop, exit_day=b["d"], days=k + 1)
        if b["h"] >= target:
            return dict(entered=True, entry=entry, entry_day=d0["d"], done=True, result="Ziel",
                        exit=target, exit_day=b["d"], days=k + 1)
    last = bars[start + hold - 1]
    return dict(entered=True, entry=entry, entry_day=d0["d"], done=True, result="Zeit (%d Tage)" % hold,
                exit=last["c"], exit_day=last["d"], days=hold)


def fee_r(cfg, entry, stop):
    """The round-trip fees in R for a full-budget position (fractional shares)."""
    s = K.size_trade(entry, stop, entry + TARGET_R * (entry - stop), cfg)
    return None if not s.get("risk_eur") else s["fees_eur"] / s["risk_eur"]


def backtest(bars, cfg, until=None):
    """Every past signal, one trade at a time (no overlapping trades)."""
    trades, i = [], LOOKBACK
    last = (until if until is not None else len(bars)) - 1
    while i < last:
        if is_signal(bars, i):
            p = plan_from(bars, i)
            if p:
                w = walk(bars[:last + 1], i + 1, p["level"], p["stop"], p["target"])
                if w.get("entered") and w.get("done"):
                    risk = p["level"] - p["stop"]
                    r = (w["exit"] - w["entry"]) / risk
                    f = fee_r(cfg, w["entry"], p["stop"]) or 0.0
                    trades.append({"day": bars[i]["d"].isoformat(), "result": w["result"], "r": r,
                                   "r_net": r - f, "days": w["days"]})
                    i = i + 1 + w["days"]
                    continue
        i += 1
    n = len(trades)
    if not n:
        return {"n": 0, "avg_r": None, "avg_r_net": None, "win": None, "trades": []}
    return {"n": n, "avg_r": round(sum(t["r"] for t in trades) / n, 3),
            "avg_r_net": round(sum(t["r_net"] for t in trades) / n, 3),
            "win": round(sum(1 for t in trades if t["r_net"] > 0) / n * 100),
            "avg_days": round(sum(t["days"] for t in trades) / n, 1), "trades": trades}


# ---------------------------------------------------------------- earnings window

def earnings_window(sym, today, fetch=None):
    """('nein' | 'gerade gemeldet' | 'erwartet' | 'unbekannt', note) from SEC filings:
    the last 8-K item 2.02 and the last 10-Q/10-K, next report about 91 days later."""
    try:
        fetch = fetch or _sec_fetch()
        cik = _cik(sym, fetch)
        if not cik:
            return "unbekannt", "keine SEC-Meldungen (z. B. ausländische Firma)"
        sub = json.loads(fetch("https://data.sec.gov/submissions/CIK%010d.json" % cik, "sub-%s.json" % sym))
    except Exception as err:  # SEC unreachable must not stop the scan
        return "unbekannt", "SEC nicht erreichbar (%s)" % str(err)[:60]
    r = (sub.get("filings") or {}).get("recent") or {}
    last_202 = last_periodic = None
    for i, form in enumerate(r.get("form") or []):
        d = datetime.date.fromisoformat(r["filingDate"][i])
        if form == "8-K" and "2.02" in (r.get("items") or [""] * (i + 1))[i]:
            last_202 = max(last_202 or d, d)
        if form in ("10-Q", "10-K"):
            last_periodic = max(last_periodic or d, d)
    if last_202 and (today - last_202).days <= 2:
        return "gerade gemeldet", "Quartalszahlen am %s" % last_202.isoformat()
    base = max([x for x in (last_202, last_periodic) if x] or [None]) if (last_202 or last_periodic) else None
    if not base:
        return "unbekannt", "keine Quartalsmeldung gefunden"
    nxt = base + datetime.timedelta(days=REPORT_EVERY)
    window_end = today + datetime.timedelta(days=14)            # 10 trading days
    if nxt - datetime.timedelta(days=REPORT_SLACK) <= window_end:
        return "erwartet", "nächste Zahlen etwa %s (letzte %s)" % (nxt.isoformat(), base.isoformat())
    return "nein", "nächste Zahlen etwa %s" % nxt.isoformat()


_CIKS = {}


def _sec_fetch():
    news = _load(os.path.join(ROOT, "trader-news", "news.py"), "trader_news_for_swing")
    ua = news.sec_user_agent(None, "trader/config.json")
    if not ua:
        raise SwingError("kein SEC User-Agent (sec_user_agent in trader/config.json oder SEC_USER_AGENT)")
    f = news.Fetcher(sec_ua=ua)
    return lambda url, name: f.get(url, name, sec=True).decode("utf-8")


def _cik(sym, fetch):
    if not _CIKS:
        for v in json.loads(fetch("https://www.sec.gov/files/company_tickers.json", "company_tickers.json")).values():
            _CIKS[str(v["ticker"]).upper()] = int(v["cik_str"])
    return _CIKS.get(sym.upper().replace("-", "."), _CIKS.get(sym.upper()))


# ---------------------------------------------------------------- one stock tonight

def check(sym, cfg, today, headline=("nein", ""), bars=None, earn=None):
    row = {"ticker": sym, "art": "swing"}
    try:
        bars = bars or daily_bars(sym)
        # the signal day is the last finished session
        i = len(bars) - 1
        if bars[i]["d"] == today and not session_over(today):
            i -= 1
        row["datum"] = bars[i]["d"].isoformat()
        row["signal"] = is_signal(bars, i)
        bt = backtest(bars, cfg, until=i + 1)
        row.update(bt_n=bt["n"], bt_r=bt["avg_r"], bt_r_net=bt["avg_r_net"], bt_win=bt["win"],
                   bt_days=bt.get("avg_days"))
        p = plan_from(bars, i)
        if not p:
            row["verdict"] = "raus (zu wenig Kursdaten)"
            return row
        row.update(level=p["level"], stop=p["stop"], target=p["target"], close=bars[i]["c"])
        size = K.size_trade(p["level"], p["stop"], p["target"], cfg)
        row.update(shares=size["shares"], risk_eur=size["risk_eur"], fees_eur=size["fees_eur"],
                   win_eur=size["win_eur"], loss_eur=size["loss_eur"], target_pct=size["target_pct"],
                   hurdle_r=size["hurdle_r"])
        row["earnings"], row["earnings_note"] = earn or earnings_window(sym, today)
        if headline[0] in ("ja", "steht an") and row["earnings"] not in ("gerade gemeldet", "erwartet"):
            row["earnings"], row["earnings_note"] = ("gerade gemeldet" if headline[0] == "ja" else "erwartet"), headline[1]
        row["verdict"] = verdict(row, cfg)
    except Exception as err:
        row["verdict"] = "raus (Fehler: %s)" % str(err)[:80]
    return row


def session_over(day):
    now = E.to_eastern(E.utc_now())
    return now.date() > day or (now.hour, now.minute) >= (16, 15)


def verdict(r, cfg):
    if not r.get("signal"):
        return "raus (kein 20-Tage-Hoch)"
    if r["earnings"] == "gerade gemeldet":
        return "raus (Zahlen gerade gemeldet)"
    if r["earnings"] == "erwartet":
        return "raus (Zahlen in der Haltezeit)"
    if r.get("hurdle_r") is None:
        return "raus (Budget zu klein)"
    if (r.get("bt_n") or 0) < MIN_TRADES:
        return "raus (zu wenig Fälle: %d)" % (r.get("bt_n") or 0)
    if r["bt_r_net"] is None or r["bt_r_net"] < cfg["fee_buffer_r"]:
        return "raus (zu schwach nach Gebühren)"
    return "Swing-Kandidat"


def next_trading_day(d):
    d = d + datetime.timedelta(days=1)
    while not E.trading_day(d):
        d += datetime.timedelta(days=1)
    return d


def as_doc(r):
    """A journal document (collection trades) for one swing plan."""
    sig = datetime.date.fromisoformat(r["datum"])
    return {"datum": r["datum"], "ticker": r["ticker"], "art": "swing",
            "ausbruch": "Kauf über %s am %s" % (K.de(r["level"]), next_trading_day(sig).strftime("%d.%m.")),
            "einstieg": r["level"], "stop": r["stop"], "ziel": r["target"], "stueck": r.get("shares"),
            "risiko_eur": r.get("risk_eur"), "gebuehren_eur": r.get("fees_eur"), "huerde_r": cfg_buffer(),
            "sim_r": None, "echt_r": r.get("bt_r_net"), "urteil": r["verdict"], "ergebnis": "",
            "ausstieg": None, "ausstieg_zeit": "", "r": None, "gewinn_eur": None,
            "bt_n": r.get("bt_n"), "bt_win": r.get("bt_win")}


def cfg_buffer():
    return K.DEFAULTS["fee_buffer_r"]


# ---------------------------------------------------------------- commands

def cmd_scan(a, cfg):
    now = E.to_eastern(E.utc_now())
    today = now.date()
    wd = K.workdir_for(cfg, today)
    if a.symbols:
        syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        uni = K.ensure_universe(wd)
        out = os.path.join(wd, "daily-swing.json")
        code, so, se = K.run_tool("scan", ["daily", "--universe", uni, "--out", out, "--show", "0", "--quiet"],
                                  timeout=1200)
        if not os.path.exists(out):
            raise SwingError("der Daily-Scan hat keine Datei geschrieben: %s" % (se or so).strip()[-200:])
        with open(out, encoding="utf-8") as fh:
            daily = json.load(fh)
        hits = [h for h in daily.get("hits") or [] if "breakout_20d" in (h.get("setups") or [])]
        if cfg["max_price_usd"] is not None:
            hits = [h for h in hits if h.get("price") and h["price"] < cfg["max_price_usd"]]
        syms = [h["symbol"] for h in hits][:cfg["max_checks"] * 2]
    names = K.universe_names(os.path.join(wd, "universe.txt"))
    heads, _ = K.news_flags(syms, names, hours=48) if syms else ({}, [])
    rows = [check(s, cfg, today, heads.get(s, ("nein", ""))) for s in syms]
    keep = [r for r in rows if r.get("level")]
    docs = {"%s_%s_swing" % (r["datum"], r["ticker"]): as_doc(r) for r in keep}
    if a.docs:
        with open(a.docs, "w", encoding="utf-8") as fh:
            json.dump(docs, fh, indent=2, ensure_ascii=False)
    if a.json:
        print(json.dumps({"rows": rows, "docs": docs}, indent=2, ensure_ascii=False, default=str))
        return 0 if any(r["verdict"] == "Swing-Kandidat" for r in rows) else 1
    print("Swing-Check %s ET: 20-Tage-Hoch, Kauf am nächsten Tag über dem Hoch, Stop 1,5 ATR, Ziel 2R, "
          "höchstens %d Handelstage." % (now.strftime("%Y-%m-%d %H:%M"), HOLD_DAYS))
    print("")
    print("| Ticker | Signal | Kauf über | Stop | Ziel | Ziel % | Stück | € am Ziel / Stop | Zahlen | "
          "Fälle (2 J.) | Ø R netto | Gewinner | Urteil |")
    print("|" + "---|" * 13)
    for r in rows:
        print("| %s | %s | %s | %s | %s | %s | %s | %s / %s | %s | %s | %s | %s | %s |" % (
            r["ticker"], r.get("datum", "-"), K.de(r.get("level")), K.de(r.get("stop")), K.de(r.get("target")),
            K.de(r.get("target_pct")), K.shares_text(r.get("shares", "-")), K.de(r.get("win_eur")),
            K.de(r.get("loss_eur")), r.get("earnings", "-"), r.get("bt_n", "-"), K.de(r.get("bt_r_net")),
            ("%s %%" % r["bt_win"]) if r.get("bt_win") is not None else "-", r["verdict"]))
    print("")
    for r in rows:
        if r.get("earnings_note"):
            print("%s: %s" % (r["ticker"], r["earnings_note"]))
    best = [r for r in rows if r["verdict"] == "Swing-Kandidat"]
    print("")
    print("Ø R netto = Durchschnitt der Regel-Trades der letzten 2 Jahre, nach Gebühren (2 € pro Trade bei %s €). "
          "Swing-Kandidat ab %d Fällen und Ø R netto mindestens %s." % (
              K.de(cfg["budget_eur"], 0), MIN_TRADES, K.de(cfg["fee_buffer_r"])))
    print("Haltezeit bis 10 Tage: Kurslücken über Nacht können den Stop überspringen. " + K.NOTE)
    if best:
        print("\nSWING-KANDIDAT: " + ", ".join("%s Kauf über %s, Stop %s, Ziel %s" % (
            r["ticker"], K.de(r["level"]), K.de(r["stop"]), K.de(r["target"])) for r in best))
    else:
        print("\nKein Swing-Kandidat.")
    return 0 if best else 1


def cmd_update(a, cfg):
    """Advance open swing documents with daily bars; write the updated ones."""
    with open(a.from_file, encoding="utf-8") as fh:
        docs = json.load(fh)
    items = docs.items() if isinstance(docs, dict) else [(d.get("_id") or "%s_%s_swing" % (d["datum"], d["ticker"]), d)
                                                          for d in docs]
    out, cache = {}, {}
    for doc_id, d in items:
        if d.get("art") != "swing" or d.get("ergebnis"):
            continue
        sym = d["ticker"]
        try:
            bars = cache.get(sym) or daily_bars(sym, "3mo")
            cache[sym] = bars
        except Exception as err:
            sys.stderr.write("%s: %s\n" % (sym, err))
            continue
        sig = datetime.date.fromisoformat(d["datum"])
        today = E.to_eastern(E.utc_now()).date()
        done_bars = [b for b in bars if b["d"] < today or session_over(b["d"])]
        start = next((i for i, b in enumerate(done_bars) if b["d"] > sig), None)
        if start is None:
            continue
        w = walk(done_bars, start, d["einstieg"], d["stop"], d["ziel"])
        new = {}
        if w.get("entered") is False:
            new = {"ergebnis": "nicht ausgelöst", "ausstieg_zeit": done_bars[start]["d"].isoformat(),
                   "r": 0.0, "gewinn_eur": 0.0}
        elif w.get("entered") and w.get("done"):
            risk = d["einstieg"] - d["stop"]
            shares = float(d.get("stueck") or 0)
            fees = float(d.get("gebuehren_eur") or 0)
            new = {"ergebnis": w["result"], "ausstieg": round(w["exit"], 2),
                   "ausstieg_zeit": w["exit_day"].isoformat(), "r": round((w["exit"] - w["entry"]) / risk, 2),
                   "gewinn_eur": round(shares * (w["exit"] - w["entry"]) / cfg["eur_usd"] - fees, 2),
                   "eingestiegen": "%s zu %s" % (w["entry_day"].isoformat(), K.de(w["entry"]))}
        elif w.get("entered"):
            new = {"eingestiegen": "%s zu %s" % (w["entry_day"].isoformat(), K.de(w["entry"])),
                   "laeuft": "Tag %d von %d" % (w["days"], HOLD_DAYS)}
        if new and any(d.get(k) != v for k, v in new.items()):
            out[doc_id] = new
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print("%d Swing-Trade(s) fortgeschrieben, Änderungen in %s." % (len(out), a.out))
    for k, v in out.items():
        print("  %s: %s" % (k, v.get("ergebnis") or v.get("laeuft") or ""))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Swing-Check (trader-kandidaten). Standard library only.")
    ap.add_argument("--config", default="trader/kandidaten.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="heutige 20-Tage-Hochs prüfen")
    p.add_argument("--symbols", help="kommagetrennte Ticker statt des Daily-Scans")
    p.add_argument("--docs", help="Tagebuch-Dokumente (collection trades) hierhin schreiben")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("update", help="offene Swing-Trades fortschreiben")
    p.add_argument("--from", dest="from_file", required=True, help="JSON: Dokumente (Liste oder nach id)")
    p.add_argument("--out", required=True, help="JSON: nur die geänderten Felder, nach id")
    a = ap.parse_args(argv)
    try:
        cfg = K.read_config(a.config)
        return {"scan": cmd_scan, "update": cmd_update}[a.cmd](a, cfg)
    except (SwingError, K.KError) as e:
        sys.stderr.write("swing.py: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
