#!/usr/bin/env python3
"""Kandidaten-Check (trader-kandidaten): the whole morning routine in one command,
for a small account, in German.

  kandidaten.py scan        ORB scan -> fresh breakouts under a price cap -> news
                            (no earnings) -> entry check, exit plan, time machine
                            -> fee hurdle -> one verdict per stock
  kandidaten.py kandidaten  the same checks on the daily scan's pre-selection
  kandidaten.py check T ... the same checks on tickers you name
  kandidaten.py tagebuch    the paper-trading journal: add, update, show, export,
                            dump / import (typed JSON documents for a journal page)

It calls the pack's own tools (scan.py, news.py, entry.py, exit.py,
timemachine.py) with --json and writes nothing inside the repo: every scan,
plan and result goes to the work folder (default /tmp/trader-kandidaten/<date>).
The journal is the one file it keeps, at the path in the config.

Verdict "Test-Kandidat" only when ALL hold: triggered, price now above the range
high, target at least min_target_pct away, no own earnings yesterday or today,
the breakout bar no older than fresh_minutes (scan only), and the time machine's
average R in the replays AND in the real past sessions at least the fee hurdle:

  hurdle = round-trip fees in EUR / risk at the stop in EUR + fee_buffer_r

A Test-Kandidat is for a paper-trading test. Nothing here places an order.
Not financial advice. Standard library only, Python 3.8+.
"""

import argparse
import concurrent.futures
import csv
import datetime
import importlib.util
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = {
    "scan": os.path.join(ROOT, "trader-scanner", "scan.py"),
    "news": os.path.join(ROOT, "trader-news", "news.py"),
    "entry": os.path.join(ROOT, "trader-entry", "entry.py"),
    "exit": os.path.join(ROOT, "trader-exit", "exit.py"),
    "tm": os.path.join(ROOT, "trader-timemachine", "timemachine.py"),
}

DEFAULTS = {
    "budget_eur": 500.0,          # the paper-trading test capital
    "fee_per_order_eur": 1.0,     # 1 EUR to buy, 1 EUR to sell
    "eur_usd": 1.15,              # dollars for one euro; set it to your broker's rate
    "fee_buffer_r": 0.10,         # on top of the fees: FX costs and stops that fill past the price
    "max_risk_pct": 2.0,          # never lose more than this share of the budget at the stop
    "max_price_usd": None,        # no cap: with fractional shares any price fits the budget
    "fractional": True,           # the broker sells fractions of a share (3 decimals)
    "min_target_pct": 2.2,
    "fresh_minutes": 10,
    "max_checks": 8,
    "since": "09:45",
    "flat_by": "15:55",
    "workdir": "/tmp/trader-kandidaten",
    "journal": "trader/tagebuch.csv",
}

JOURNAL_FIELDS = ["datum", "ticker", "ausbruch", "einstieg", "stop", "ziel", "stueck",
                  "risiko_eur", "gebuehren_eur", "huerde_r", "sim_r", "echt_r", "urteil",
                  "ergebnis", "ausstieg", "ausstieg_zeit", "r", "gewinn_eur", "markt", "vol_x"]

NOTE = ("Ein Test-Kandidat ist nur für einen Test im Paper-Trading gedacht. "
        "Keine Kaufempfehlung, not financial advice. Es wurde nichts gekauft.")


class KError(Exception):
    pass


# ---------------------------------------------------------------- helpers

def _load_entry():
    spec = importlib.util.spec_from_file_location("trader_entry_for_kandidaten", TOOLS["entry"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ENTRY = None


def entry_mod():
    global _ENTRY
    if _ENTRY is None:
        _ENTRY = _load_entry()
    return _ENTRY


def read_config(path):
    cfg = dict(DEFAULTS)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except ValueError as e:
            raise KError("%s ist kein gültiges JSON: %s" % (path, e))
        for k in DEFAULTS:
            if k in doc and doc[k] is not None:
                cfg[k] = doc[k]
    for k in ("budget_eur", "fee_per_order_eur", "eur_usd", "fee_buffer_r", "min_target_pct", "max_risk_pct"):
        cfg[k] = float(cfg[k])
    cfg["max_price_usd"] = None if cfg["max_price_usd"] in (None, "", 0) else float(cfg["max_price_usd"])
    cfg["fractional"] = bool(cfg["fractional"])
    for k in ("fresh_minutes", "max_checks"):
        cfg[k] = int(cfg[k])
    if cfg["eur_usd"] <= 0 or cfg["budget_eur"] <= 0:
        raise KError("budget_eur und eur_usd müssen größer als 0 sein")
    return cfg


def minutes_of(text):
    """'10:05 AM', '10:05', '2026-11-09 10:15 ET' -> minutes after midnight."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])?", str(text or ""))
    if not m:
        raise KError("keine Uhrzeit in %r" % text)
    h, mi = int(m.group(1)), int(m.group(2))
    ap = (m.group(3) or "").upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return h * 60 + mi


def hhmm(minutes):
    return "%d:%02d" % (minutes // 60, minutes % 60)


def de(x, nd=2):
    """German decimal comma."""
    if x is None:
        return "-"
    return ("%.*f" % (nd, x)).replace(".", ",")


def run_tool(name, args, timeout=600):
    """Run one of the pack's tools; return (exit code, stdout, stderr)."""
    cmd = [sys.executable, TOOLS[name]] + [str(a) for a in args]
    try:
        p = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise KError("%s hat zu lange gebraucht" % name)
    return p.returncode, p.stdout, p.stderr


def run_json(name, args, timeout=600):
    code, out, err = run_tool(name, list(args) + ["--json"], timeout)
    try:
        return json.loads(out)
    except ValueError:
        msg = (err or out or "").strip().splitlines()
        raise KError("%s: %s" % (name, msg[-1] if msg else "keine Ausgabe (exit %d)" % code))


def ny_now(now_text=None):
    em = entry_mod()
    utc = em.parse_now(now_text) if now_text else em.utc_now()
    return em.to_eastern(utc)


def workdir_for(cfg, day):
    d = os.path.join(cfg["workdir"], day.isoformat())
    for sub in ("", "plans"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    return d


# ---------------------------------------------------------------- money and the hurdle

def size_trade(entry, stop, target, cfg):
    """Shares the budget buys (whole, or fractions to 3 decimals when the broker allows them),
    no more than keeps the loss at the stop within max_risk_pct of the budget,
    risk and P/L in EUR after fees, the fee hurdle in R."""
    budget_usd = cfg["budget_eur"] * cfg["eur_usd"]
    risk_ps = entry - stop
    cap = budget_usd / entry if entry > 0 else 0
    if risk_ps > 0 and cfg.get("max_risk_pct"):
        # the loss at the stop may not exceed max_risk_pct of the budget
        cap = min(cap, cfg["budget_eur"] * cfg["max_risk_pct"] / 100.0 * cfg["eur_usd"] / risk_ps)
    if entry <= 0:
        shares = 0
    elif cfg.get("fractional"):
        shares = math.floor(cap * 1000) / 1000.0
    else:
        shares = int(math.floor(cap))
    fees = 2 * cfg["fee_per_order_eur"]
    out = {"shares": shares, "fees_eur": round(fees, 2), "stop_pct": round(risk_ps / entry * 100, 2),
           "target_pct": round((target - entry) / entry * 100, 2)}
    if shares <= 0 or risk_ps <= 0:
        out.update(risk_eur=None, win_eur=None, loss_eur=None, hurdle_r=None)
        return out
    risk_eur = shares * risk_ps / cfg["eur_usd"]
    win_eur = shares * (target - entry) / cfg["eur_usd"] - fees
    out.update(risk_eur=round(risk_eur, 2), win_eur=round(win_eur, 2), loss_eur=round(-risk_eur - fees, 2),
               hurdle_r=round(fees / risk_eur + cfg["fee_buffer_r"], 2))
    return out


# ---------------------------------------------------------------- earnings in the news

AHEAD = ("ahead of earnings", "to report", "will report", "reports soon", "earnings preview",
         "set to report", "before earnings", "ahead of its earnings", "ahead of q")
# words that say the headline is about a fresh report, not an opinion piece that mentions earnings
REPORTED = re.compile(r"\b(reports? (q[1-4]|first|second|third|fourth|fiscal|quarter|earnings|results|"
                      r"profit|loss|revenue|sales)|reported|results|beats?|miss(es|ed)?|tops?|surpass(es|ed)?|"
                      r"lags?|snapshot|earnings call|guidance|q[1-4]\b|quarter(ly)?|fiscal)", re.I)
STOP_WORDS = {"the", "inc", "inc.", "corp", "corp.", "co", "co.", "ltd", "plc", "holdings", "group",
              "company", "class", "common", "stock", "shares", "and", "of", "a"}


def name_word(name):
    for w in re.findall(r"[A-Za-z][A-Za-z&'\-]+", name or ""):
        if w.lower() not in STOP_WORDS and len(w) >= 3:
            return w
    return None


def earnings_flag(sym, items, name=None):
    """('ja' | 'steht an' | 'nein', reason) for the stock's OWN earnings in news items.
    A filing with 8-K item 2.02 is earnings. A headline tagged earnings counts only
    when it names the company (ticker or the first real word of its name) and reads
    like a report (results, beats, Q3, earnings call ...) or like one coming up.
    A heuristic: it errs toward 'ja', which keeps a stock out."""
    sym = sym.upper()
    word = name_word(name)
    ahead = None
    for it in items:
        if sym not in [t.upper() for t in (it.get("touches") or [it.get("ticker") or ""])]:
            continue
        title = it.get("title") or ""
        if it.get("kind") == "filing" and "2.02" in (it.get("items") or []):
            return "ja", "8-K mit Quartalszahlen (Item 2.02)"
        if "earnings" not in (it.get("tags") or []):
            continue
        low = title.lower()
        names_it = re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(sym), title) or \
            (word and word.lower() in low)
        if not names_it:
            continue
        if any(a in low for a in AHEAD):
            ahead = ahead or ("steht an", title[:90])
            continue
        if REPORTED.search(title):
            return "ja", title[:90]
    return ahead or ("nein", "")


def universe_names(path):
    names = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "|" in line:
                    s, n = line.rstrip("\n").split("|", 1)
                    names[s.strip().upper()] = n.strip()
    return names


# ---------------------------------------------------------------- one stock

def verdict(r, cfg, fresh_required):
    """r: a checked row. Returns the German verdict text."""
    if r.get("fixed"):
        return r["fixed"]
    if r.get("error"):
        return "raus (Fehler: %s)" % r["error"]
    if r["status"] != "triggered":
        if r.get("phase") in ("forming", "pre-open"):
            return "offen (Range ab 9:45)"
        if r["status"] == "waiting":
            return "offen (noch kein Ausbruch)"
        return "raus (%s)" % {"expired": "abgelaufen", "no-data": "keine Daten"}.get(r["status"], r["status"])
    if fresh_required and r.get("age_min") is not None and r["age_min"] > cfg["fresh_minutes"]:
        return "raus (zu alt)"
    if not r.get("above_high"):
        return "raus (unter Range-Hoch)"
    if r.get("target_pct") is None or r["target_pct"] < cfg["min_target_pct"]:
        return "raus (Ziel < %s %%)" % de(cfg["min_target_pct"], 1)
    if r.get("earnings") in ("ja", "steht an"):
        return "raus (Zahlen %s)" % ("heute/gestern" if r["earnings"] == "ja" else "stehen an")
    if r.get("earnings") == "?":
        return "raus (News nicht abrufbar)"
    if r.get("hurdle_r") is None:
        return "raus (Budget zu klein für 1 Aktie)"
    if r.get("sim_r") is None or r.get("hist_r") is None:
        return "raus (Zeitmaschine ohne Ergebnis)"
    if r["sim_r"] < r["hurdle_r"] or r["hist_r"] < r["hurdle_r"]:
        return "raus (zu schwach nach Gebühren)"
    return "Test-Kandidat"


def check_symbol(sym, cfg, day, wd, earnings=("nein", ""), scan_hit=None, scan_min=None, now_text=None):
    """Entry check -> plan -> time machine for one ticker. Never raises."""
    row = {"ticker": sym, "status": "-", "earnings": earnings[0], "earnings_note": earnings[1]}
    try:
        extra = ["--now", now_text] if now_text else []
        e = run_json("entry", ["check", sym, "--setup", "orb"] + extra)
        row["status"] = e.get("status", "-")
        row["phase"] = e.get("phase")
        orr = e.get("opening_range") or {}
        row["or_high"], row["or_low"] = orr.get("high"), orr.get("low")
        last = (e.get("forming") or {}).get("price")
        if last is None:
            last = (e.get("last") or {}).get("close")
        row["price_now"] = last
        row["now"] = e.get("now")
        t = e.get("trigger") or {}
        if row["status"] != "triggered" or not t:
            return row
        row["entry"], row["entry_time"] = t.get("price"), t.get("time")
        row["vol_x"] = t.get("volume_x")          # breakout bar volume / opening-range average
        if scan_min is not None and t.get("time"):
            row["age_min"] = scan_min - minutes_of(t["time"])
        row["above_high"] = last is not None and row["or_high"] is not None and last > row["or_high"]
        plan_path = os.path.join(wd, "plans", "%s-%s.json" % (sym, day.isoformat()))
        p = run_json("exit", ["plan", "--symbol", sym, "--setup", "orb", "--entry", row["entry"],
                              "--or-high", row["or_high"], "--or-low", row["or_low"],
                              "--time", row["entry_time"], "--date", day.isoformat(),
                              "--stop", "or-mid", "--target", "2R", "--write", plan_path] + extra)
        row["stop"], row["target"] = p.get("stop"), p.get("target")
        row.update(size_trade(row["entry"], row["stop"], row["target"], cfg))
        try:
            tm = run_json("tm", ["run", "--plan", plan_path] + extra, timeout=900)
            row["sim_r"] = (tm.get("mc") or {}).get("avg_R")
            h = tm.get("history") or {}
            row["hist_r"], row["hist_n"] = h.get("avg_R"), h.get("triggered")
        except KError as err:
            row["tm_error"] = str(err)
    except KError as err:
        row["error"] = str(err)
    return row


def check_all(symbols, cfg, day, wd, earnings, scan_min=None, now_text=None, workers=8):
    rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, len(symbols) or 1))) as ex:
        futs = {ex.submit(check_symbol, s, cfg, day, wd, earnings.get(s, ("nein", "")), None, scan_min, now_text): s
                for s in symbols}
        for f in concurrent.futures.as_completed(futs):
            rows.append(f.result())
    order = {s: i for i, s in enumerate(symbols)}
    rows.sort(key=lambda r: order.get(r["ticker"], 99))
    return rows


EXCLUDED = {"ja": "raus (Zahlen heute/gestern)", "steht an": "raus (Zahlen stehen an)",
            "?": "raus (News nicht abrufbar)"}


def news_flags(symbols, names, hours=24, chunk=10):
    """Earnings flags per ticker, asking news.py in small batches. A batch that fails
    marks its tickers '?' (unknown), which keeps them from being a Test-Kandidat."""
    flags, notes = {}, []
    symbols = list(symbols)
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        try:
            d = run_json("news", ["--tickers"] + part + ["--hours", hours, "--dry-run"])
        except KError as err:
            for s in part:
                flags[s] = ("?", "News nicht abrufbar: %s" % str(err)[-80:])
            continue
        items = d.get("items") or []
        notes.extend(d.get("notes") or [])
        for s in part:
            flags[s] = earnings_flag(s, items, names.get(s))
    return flags, notes


# ---------------------------------------------------------------- scan windows

def fresh_window(hits, scan_min, cfg, since_min, max_price):
    """Split scan hits into (fresh, too_old) inside the window since -> scan time, under the price cap."""
    fresh, old = [], []
    for h in hits:
        if h.get("price") is None or (max_price is not None and h["price"] >= max_price):
            continue
        at = minutes_of(h.get("at"))
        if at <= since_min or at > scan_min:
            continue
        (fresh if scan_min - at <= cfg["fresh_minutes"] else old).append(h)
    fresh.sort(key=lambda h: minutes_of(h["at"]), reverse=True)
    return fresh, old


def ensure_universe(wd):
    path = os.path.join(wd, "universe.txt")
    if not os.path.exists(path):
        code, out, err = run_tool("scan", ["universe", "--out", path])
        if code != 0 or not os.path.exists(path):
            raise KError("Aktienliste konnte nicht geladen werden: %s" % (err or out).strip()[-200:])
    return path


# ---------------------------------------------------------------- output

def price_text(cfg):
    return "" if cfg["max_price_usd"] is None else " unter %s $" % de(cfg["max_price_usd"], 0)


def shares_text(x):
    if x is None or x == "-":
        return "-"
    return str(int(x)) if float(x).is_integer() else de(x, 3)


def table(rows, cfg, title, extra_lines=()):
    out = [title, ""]
    out.append("| Ticker | Status | Ausbruch | Einstieg | Stop | Ziel | Ziel % | Kurs jetzt | über Hoch | "
               "Zahlen | Vol × | Ø R Sim / echt | Hürde | Stück | € am Ziel / Stop | Urteil |")
    out.append("|" + "---|" * 16)
    for r in rows:
        st = {"triggered": "ausgelöst", "waiting": "wartet", "expired": "abgelaufen",
              "no-data": "keine Daten"}.get(r.get("status"), r.get("status", "-"))
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s / %s | %s | %s | %s / %s | %s |" % (
            r["ticker"], st, r.get("entry_time") or "-", de(r.get("entry")), de(r.get("stop")),
            de(r.get("target")), de(r.get("target_pct")), de(r.get("price_now")),
            "-" if r.get("above_high") is None else ("ja" if r["above_high"] else "nein"),
            r.get("earnings", "-"), de(r.get("vol_x"), 1), de(r.get("sim_r")), de(r.get("hist_r")), de(r.get("hurdle_r")),
            shares_text(r.get("shares", "-")), de(r.get("win_eur")), de(r.get("loss_eur")), r.get("verdict", "-")))
    out.append("")
    out.extend(extra_lines)
    out.append("Rechnung: %s € Budget, %s € pro Kauf und pro Verkauf, 1 € = %s $, %s. Hürde = Gebühren ÷ "
               "Risiko in € + %s R." % (de(cfg["budget_eur"], 0), de(cfg["fee_per_order_eur"]), de(cfg["eur_usd"]),
                                       "Bruchstücke erlaubt" if cfg.get("fractional") else "nur ganze Aktien",
                                       de(cfg["fee_buffer_r"])))
    out.append("„Zahlen: nein“ heißt nur: keine Schlagzeile dazu. Das News-Tool hat keinen Terminkalender.")
    out.append(NOTE)
    return "\n".join(out)


def market_now():
    """The market light (markt.py status), or None when it cannot be read."""
    try:
        spec = importlib.util.spec_from_file_location("trader_markt_for_kandidaten", os.path.join(HERE, "markt.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.status()
    except Exception:
        return None


def finish(rows, cfg, title, args, wd, extra_lines=(), stamp="check"):
    fresh_required = getattr(args, "cmd", "") == "scan"
    mk = None if getattr(args, "no_market", False) else market_now()
    if mk:
        for r in rows:
            r["markt"] = mk["ampel"]
        extra_lines = ["Markt: Ampel %s (%s). Die Ampel ist eine Information, kein Filter." % (
            mk["ampel"].upper(), mk["grund"])] + list(extra_lines)
    for r in rows:
        r["verdict"] = verdict(r, cfg, fresh_required)
    tests = [r for r in rows if r["verdict"] == "Test-Kandidat"]
    doc = {"created_by": "trader-kandidaten", "mode": getattr(args, "cmd", ""), "title": title, "markt": mk,
           "config": {k: cfg[k] for k in ("budget_eur", "fee_per_order_eur", "eur_usd", "fee_buffer_r",
                                          "max_price_usd", "fractional", "min_target_pct", "fresh_minutes")},
           "rows": rows, "test_kandidaten": [r["ticker"] for r in tests]}
    path = os.path.join(wd, "ergebnis-%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    if getattr(args, "journal", None) is not None:
        jpath = args.journal or cfg["journal"]
        added = journal_add(jpath, rows, all_rows=args.journal_all)
        extra_lines = list(extra_lines) + ["Tagebuch: %d Zeile(n) in %s eingetragen." % (added, jpath)]
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    else:
        print(table(rows, cfg, title, extra_lines))
        if tests:
            print("\nTEST-KANDIDAT: " + ", ".join("%s Einstieg %s, Stop %s, Ziel %s, jetzt %s" % (
                r["ticker"], de(r["entry"]), de(r["stop"]), de(r["target"]), de(r.get("price_now"))) for r in tests))
        else:
            print("\nKein Test-Kandidat.")
        print("Ergebnis gespeichert: %s" % path)
    return 0 if tests else 1


# ---------------------------------------------------------------- commands

def cmd_scan(a, cfg):
    now = ny_now(a.now)
    day = now.date()
    wd = workdir_for(cfg, day)
    if a.from_file:
        with open(a.from_file, encoding="utf-8") as fh:
            scan = json.load(fh)
    else:
        uni = ensure_universe(wd)
        out = os.path.join(wd, "orb-%s.json" % now.strftime("%H%M"))
        args = ["orb", "--universe", uni, "--out", out, "--show", "0", "--quiet"]
        if a.now:
            args += ["--now", a.now]
        code, so, se = run_tool("scan", args, timeout=1200)
        if not os.path.exists(out):
            raise KError("der Scan hat keine Datei geschrieben: %s" % (se or so).strip()[-300:])
        with open(out, encoding="utf-8") as fh:
            scan = json.load(fh)
    scan_min = minutes_of(scan.get("as_of"))
    since = minutes_of(a.since or cfg["since"])
    fresh, old = fresh_window(scan.get("hits") or [], scan_min, cfg, since, cfg["max_price_usd"])
    names = universe_names(os.path.join(wd, "universe.txt"))
    syms = [h["symbol"] for h in fresh]
    flags, notes = news_flags(syms, names)
    keep = [s for s in syms if flags.get(s, ("nein",))[0] == "nein"][:cfg["max_checks"]]
    rows = check_all(keep, cfg, day, wd, flags, scan_min=scan_min, now_text=a.now)
    for s in syms:
        if s not in keep and flags.get(s, ("nein",))[0] != "nein":
            rows.append({"ticker": s, "status": "triggered", "earnings": flags[s][0], "earnings_note": flags[s][1],
                         "fixed": EXCLUDED.get(flags[s][0], "raus")})
    lines = ["Scan %s: %d Treffer, im Fenster ab %s%s: %d frisch, %d zu alt (%s)." % (
        scan.get("as_of"), len(scan.get("hits") or []), hhmm(since), price_text(cfg),
        len(fresh), len(old), " ".join(h["symbol"] for h in old) or "-")]
    if scan.get("unconfirmed"):
        lines.append("Hinweis: %s weitere Kandidaten hat der Scanner nicht genau geprüft." % scan["unconfirmed"])
    for s in syms:
        if flags.get(s, ("nein",))[0] != "nein":
            lines.append("%s: Zahlen %s (%s)" % (s, flags[s][0], flags[s][1]))
    return finish(rows, cfg, "Kandidaten-Check (Scan) %s ET" % now.strftime("%Y-%m-%d %H:%M"), a, wd, lines,
                  stamp="scan-%s" % now.strftime("%H%M"))


def cmd_kandidaten(a, cfg):
    now = ny_now(a.now)
    day = now.date()
    wd = workdir_for(cfg, day)
    uni = ensure_universe(wd)
    out = os.path.join(wd, "daily.json")
    if not os.path.exists(out) or a.fresh:
        code, so, se = run_tool("scan", ["daily", "--universe", uni, "--out", out, "--show", "0", "--quiet"],
                                timeout=1200)
        if not os.path.exists(out):
            raise KError("der Daily-Scan hat keine Datei geschrieben: %s" % (se or so).strip()[-300:])
    with open(out, encoding="utf-8") as fh:
        daily = json.load(fh)
    hits = [h for h in daily.get("hits") or [] if h.get("price") is not None
            and (cfg["max_price_usd"] is None or h["price"] < cfg["max_price_usd"])]
    # the daily scan lists the most traded first; news for a few more than we check, as spares
    syms = [h["symbol"] for h in hits][:cfg["max_checks"] * 3]
    flags, notes = news_flags(syms, universe_names(uni))
    keep = [s for s in syms if flags.get(s, ("nein",))[0] == "nein"][:cfg["max_checks"]]
    rows = check_all(keep, cfg, day, wd, flags, now_text=a.now)
    gone = [s for s in syms if flags.get(s, ("nein",))[0] != "nein"]
    lines = ["Daily-Scan (%s): %d Treffer%s, die %d meistgehandelten angesehen, %d wegen Zahlen "
             "oder fehlender News gestrichen (%s), %d geprüft." % (daily.get("as_of"), len(hits), price_text(cfg),
                                               len(syms), len(gone), " ".join(gone) or "-", len(keep))]
    if now.hour * 60 + now.minute < 9 * 60 + 50:
        lines.append("Vor 9:50 ET steht die Range noch nicht: Ausbruch, Ziel und Zeitmaschine kommen erst danach.")
    return finish(rows, cfg, "Kandidaten %s ET" % now.strftime("%Y-%m-%d %H:%M"), a, wd, lines,
                  stamp="kandidaten-%s" % now.strftime("%H%M"))


def cmd_check(a, cfg):
    now = ny_now(a.now)
    day = now.date()
    wd = workdir_for(cfg, day)
    syms = [s.upper() for s in a.tickers]
    flags, notes = news_flags(syms, universe_names(ensure_universe(wd)))
    rows = check_all(syms, cfg, day, wd, flags, now_text=a.now)
    return finish(rows, cfg, "Kandidaten-Check %s ET" % now.strftime("%Y-%m-%d %H:%M"), a, wd,
                  stamp="check-%s" % now.strftime("%H%M"))


# ---------------------------------------------------------------- the journal

def journal_read(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def journal_write(path, rows):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in JOURNAL_FIELDS})


def _num(x):
    return "" if x is None else ("%.2f" % x if isinstance(x, float) else str(x))


def _shares(x):
    if x is None:
        return ""
    return str(int(x)) if float(x).is_integer() else ("%.3f" % x).rstrip("0").rstrip(".")


def journal_add(path, rows, day=None, all_rows=False):
    """Add Test-Kandidaten (or every planned trade with all_rows) not already in the journal."""
    have = journal_read(path)
    keys = {(r["datum"], r["ticker"]) for r in have}
    added = 0
    for r in rows:
        if not r.get("stop") or not r.get("entry"):
            continue
        if not all_rows and r.get("verdict") != "Test-Kandidat":
            continue
        d = (r.get("now") or day or "")[:10]
        if (d, r["ticker"]) in keys:
            continue
        have.append({"datum": d, "ticker": r["ticker"], "ausbruch": r.get("entry_time", ""),
                     "einstieg": _num(r.get("entry")), "stop": _num(r.get("stop")), "ziel": _num(r.get("target")),
                     "stueck": _shares(r.get("shares")), "risiko_eur": _num(r.get("risk_eur")),
                     "gebuehren_eur": _num(r.get("fees_eur")), "huerde_r": _num(r.get("hurdle_r")),
                     "sim_r": _num(r.get("sim_r")), "echt_r": _num(r.get("hist_r")),
                     "urteil": r.get("verdict", ""), "markt": r.get("markt", ""),
                     "vol_x": _num(r.get("vol_x"))})
        keys.add((d, r["ticker"]))
        added += 1
    journal_write(path, have)
    return added


def outcome(bars, entry_close_min, entry, stop, target, flat_min):
    """Walk 5-minute bars (dicts with 'm' = bar start in minutes, h, l, c) after the entry.
    Stop and target in the same bar count as the stop (the cautious reading).
    Returns (result, exit price, exit time minutes) or None while the day is not done."""
    after = [b for b in bars if b["m"] >= entry_close_min]
    for b in after:
        if b["m"] + 5 > flat_min:
            break
        if b["l"] <= stop:
            return "Stop", stop, b["m"]
        if b["h"] >= target:
            return "Ziel", target, b["m"]
    last = [b for b in after if b["m"] + 5 <= flat_min]
    if last and last[-1]["m"] + 5 >= flat_min:
        return "Schluss %s" % hhmm(flat_min), last[-1]["c"], last[-1]["m"] + 5
    return None


def journal_update(path, cfg, now_text=None, provider=None):
    """Fill in results for open rows whose day is finished."""
    em = entry_mod()
    rows = journal_read(path)
    now = ny_now(now_text)
    flat_min = minutes_of(cfg["flat_by"])
    done = 0
    for r in rows:
        if r.get("ergebnis"):
            continue
        day = datetime.date.fromisoformat(r["datum"])
        if day > now.date() or (day == now.date() and now.hour * 60 + now.minute < flat_min):
            continue
        get = provider or (lambda sym, d: em.get_chart(sym, "5m", day=d, today=now.date()))
        try:
            ch = get(r["ticker"], day)
        except Exception as err:  # one bad ticker must not stop the rest
            r["ergebnis"] = ""
            sys.stderr.write("%s: %s\n" % (r["ticker"], err))
            continue
        bars = []
        for b in ch["bars"]:
            loc = em.to_eastern(em.utc_of(b["t"]))
            if loc.date() == day:
                bars.append({"m": loc.hour * 60 + loc.minute, "h": b["h"], "l": b["l"], "c": b["c"]})
        entry, stop, target = float(r["einstieg"]), float(r["stop"]), float(r["ziel"])
        res = outcome(bars, minutes_of(r["ausbruch"]), entry, stop, target, flat_min)
        if not res:
            continue
        kind, px, m = res
        shares = float(r["stueck"] or 0)
        fees = float(r["gebuehren_eur"] or 0)
        r["ergebnis"], r["ausstieg"], r["ausstieg_zeit"] = kind, "%.2f" % px, hhmm(m)
        r["r"] = "%.2f" % ((px - entry) / (entry - stop))
        r["gewinn_eur"] = "%.2f" % (shares * (px - entry) / cfg["eur_usd"] - fees)
        done += 1
    journal_write(path, rows)
    return done, rows


def journal_summary(rows):
    closed = [r for r in rows if r.get("ergebnis")]
    eur = sum(float(r["gewinn_eur"]) for r in closed if r.get("gewinn_eur"))
    rs = [float(r["r"]) for r in closed if r.get("r")]
    wins = sum(1 for r in closed if r.get("gewinn_eur") and float(r["gewinn_eur"]) > 0)
    return {"trades": len(rows), "closed": len(closed), "open": len(rows) - len(closed), "wins": wins,
            "eur": round(eur, 2), "avg_r": round(sum(rs) / len(rs), 2) if rs else None}


def journal_export(rows, out_path):
    """CSV for Numbers or Excel in German: semicolons and decimal commas."""
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(JOURNAL_FIELDS)
        for r in rows:
            vals = []
            for k in JOURNAL_FIELDS:
                v = r.get(k, "") or ""
                if re.fullmatch(r"-?\d+\.\d+", v):
                    v = v.replace(".", ",")
                vals.append(v)
            w.writerow(vals)


NUMERIC = {"einstieg", "stop", "ziel", "risiko_eur", "gebuehren_eur", "huerde_r", "sim_r", "echt_r",
           "ausstieg", "r", "gewinn_eur", "vol_x"}


def doc_id(r):
    return "%s_%s" % (r["datum"], r["ticker"])


def journal_docs(rows):
    """Journal rows as typed JSON documents keyed by date_ticker (for a journal page's store)."""
    out = {}
    for r in rows:
        d = {}
        for k in JOURNAL_FIELDS:
            v = r.get(k, "")
            if k in NUMERIC:
                d[k] = float(v) if v not in ("", None) else None
            elif k == "stueck":
                if v in ("", None):
                    d[k] = None
                else:
                    f = float(v)
                    d[k] = int(f) if f.is_integer() else f
            else:
                d[k] = v if v is not None else ""
        out[doc_id(r)] = d
    return out


def journal_import(path, docs):
    """Merge documents (a list, or a dict keyed by id) into the CSV journal; the documents win."""
    rows = {doc_id(r): r for r in journal_read(path)}
    items = docs.values() if isinstance(docs, dict) else docs
    for d in items:
        if d.get("art") == "swing":        # swing trades run for days: swing.py update handles them
            continue
        r = {k: ("" if d.get(k) is None else (_shares(d[k]) if k == "stueck" else _num(d[k]) if isinstance(d.get(k), (int, float))
                                             else str(d[k]))) for k in JOURNAL_FIELDS}
        rows[doc_id(r)] = r
    merged = sorted(rows.values(), key=lambda r: (r["datum"], r["ticker"]))
    journal_write(path, merged)
    return len(merged)


def cmd_tagebuch(a, cfg):
    path = a.path or cfg["journal"]
    if a.action == "dump":
        print(json.dumps(journal_docs(journal_read(path)), indent=2, ensure_ascii=False))
        return 0
    if a.action == "import":
        with open(a.from_file, encoding="utf-8") as fh:
            n = journal_import(path, json.load(fh))
        print("%s hat jetzt %d Zeile(n)." % (path, n))
        return 0
    if a.action == "add":
        with open(a.from_file, encoding="utf-8") as fh:
            doc = json.load(fh)
        n = journal_add(path, doc.get("rows") or [], all_rows=a.all)
        print("%d Zeile(n) in %s eingetragen." % (n, path))
        return 0
    if a.action == "update":
        n, rows = journal_update(path, cfg, a.now)
        print("%d Trade(s) ausgewertet." % n)
    rows = journal_read(path)
    if a.action == "export":
        out = a.out or os.path.splitext(path)[0] + "-numbers.csv"
        journal_export(rows, out)
        print("Exportiert für Numbers/Excel: %s (%d Zeilen)" % (out, len(rows)))
        return 0
    s = journal_summary(rows)
    if a.json:
        print(json.dumps({"summary": s, "rows": rows}, indent=2, ensure_ascii=False))
        return 0
    print("Tagebuch %s: %d Trades, %d ausgewertet, %d offen." % (path, s["trades"], s["closed"], s["open"]))
    for r in rows[-20:]:
        print("  %s %-6s %s Einstieg %s Stop %s Ziel %s  %s %s  %s €" % (
            r["datum"], r["ticker"], r["ausbruch"], r["einstieg"], r["stop"], r["ziel"],
            r.get("ergebnis") or "offen", r.get("ausstieg_zeit") or "", r.get("gewinn_eur") or "-"))
    if s["closed"]:
        print("Summe nach Gebühren: %s € | Gewinner %d von %d | Ø R %s" % (
            de(s["eur"]), s["wins"], s["closed"], de(s["avg_r"])))
    print("Paper-Trading-Tagebuch: Ergebnisse nach Regel ausgerechnet, nichts davon wurde gehandelt.")
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Kandidaten-Check (trader-kandidaten). Standard library only.")
    ap.add_argument("--config", default="trader/kandidaten.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--now", help='so tun, als wäre es diese New Yorker Zeit ("2026-11-09 10:15")')
        p.add_argument("--json", action="store_true")
        p.add_argument("--journal", nargs="?", const="", metavar="PATH",
                       help="Test-Kandidaten ins Tagebuch eintragen (Pfad aus der Config, wenn leer)")
        p.add_argument("--journal-all", action="store_true", help="jeden geplanten Trade eintragen, nicht nur Test-Kandidaten")
        p.add_argument("--no-market", action="store_true", help="die Markt-Ampel nicht abfragen")

    p = sub.add_parser("scan", help="ORB-Scan, frische Ausbrüche prüfen")
    p.add_argument("--since", help="nur Ausbruchskerzen, die nach HH:MM geschlossen haben (Standard 09:45)")
    p.add_argument("--from", dest="from_file", metavar="ORB.json", help="einen gespeicherten Scan nehmen, nicht neu scannen")
    common(p)
    p = sub.add_parser("kandidaten", help="Vorauswahl aus dem Daily-Scan prüfen")
    p.add_argument("--fresh", action="store_true", help="den Daily-Scan neu laden")
    common(p)
    p = sub.add_parser("check", help="bestimmte Ticker prüfen")
    p.add_argument("tickers", nargs="+")
    common(p)
    p = sub.add_parser("tagebuch", help="Paper-Trading-Tagebuch")
    p.add_argument("action", choices=["show", "add", "update", "export", "dump", "import"])
    p.add_argument("--path", help="Tagebuch-Datei (Standard aus der Config)")
    p.add_argument("--from", dest="from_file", help="add: ergebnis-*.json; import: JSON-Dokumente (Liste oder nach id)")
    p.add_argument("--all", action="store_true", help="add: jeden geplanten Trade, nicht nur Test-Kandidaten")
    p.add_argument("--out", help="export: Zieldatei")
    p.add_argument("--now")
    p.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    try:
        cfg = read_config(a.config)
        if a.cmd == "tagebuch":
            if a.action in ("add", "import") and not a.from_file:
                raise KError("tagebuch %s braucht --from DATEI" % a.action)
            return cmd_tagebuch(a, cfg)
        return {"scan": cmd_scan, "kandidaten": cmd_kandidaten, "check": cmd_check}[a.cmd](a, cfg)
    except KError as e:
        sys.stderr.write("kandidaten.py: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
