#!/usr/bin/env python3
"""Markt-Lage (trader-kandidaten): what the whole market is doing, and what just broke.

  markt.py status            the weather: SPY and QQQ against their 20-day average,
                             today's move, the VIX, which sectors lead and lag,
                             and one light: gruen / gelb / rot
  markt.py radar             fresh SEC 8-K filings of listed companies that move
                             stocks (earnings, deals, CEO changes, bankruptcy ...)
                             and today's Nasdaq trading halts, newest first
  markt.py termine --from F  market-wide dates (Fed, CPI, jobs report) from a JSON
                             list, the ones in the next days

The light, kept simple so it can be tested against the journal later:
  rot    VIX at 25 or more, or SPY down 1 % or more today,
         or SPY under its 20-day average and down today
  gruen  SPY over its 20-day average, not down more than 0.2 % today, VIX under 20
  gelb   everything else

Reads only public endpoints the pack already uses (Yahoo charts, SEC EDGAR,
Nasdaq Trader). Information, never an order. Not financial advice.
Standard library only, Python 3.8+.
"""

import argparse
import datetime
import html
import importlib.util
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SECTORS = [("XLK", "Technologie"), ("SMH", "Halbleiter"), ("XLC", "Kommunikation"), ("XLY", "Konsum (zyklisch)"),
           ("XLP", "Konsum (Basis)"), ("XLF", "Finanzen"), ("XLV", "Gesundheit"), ("XLI", "Industrie"),
           ("XLE", "Energie"), ("XLB", "Rohstoffe"), ("XLU", "Versorger"), ("XLRE", "Immobilien")]

# 8-K items worth a look, in plain German
ITEMS = {"1.01": "Wichtiger Vertrag / Deal", "1.02": "Vertrag beendet", "1.03": "Insolvenz",
         "2.01": "Übernahme / Verkauf abgeschlossen", "2.02": "Quartalszahlen", "2.05": "Restrukturierung / Stellenabbau",
         "2.06": "Wertberichtigung", "3.01": "Börsennotiz gefährdet", "4.01": "Wirtschaftsprüfer gewechselt",
         "4.02": "Frühere Bilanzen nicht verlässlich", "5.01": "Kontrollwechsel", "5.02": "Wechsel im Management",
         "7.01": "Mitteilung an Investoren", "8.01": "Sonstige wichtige Ereignisse"}
STRONG = {"1.03", "2.02", "4.02", "5.01", "2.01", "1.01", "2.06", "3.01", "2.05", "5.02"}

# Nasdaq halt reason codes, the common ones
HALTS = {"T1": "Stopp: Nachricht kommt gleich", "T2": "Stopp: Nachricht veröffentlicht",
         "T5": "Stopp: Einzelaktie (Kursschwankung)", "T6": "Stopp: ungewöhnliche Marktaktivität",
         "T8": "Stopp: ETF", "T12": "Stopp: weitere Infos angefordert", "H4": "Stopp: Börsenregeln nicht erfüllt",
         "H9": "Stopp: Unterlagen fehlen", "H10": "Stopp: SEC hat den Handel ausgesetzt",
         "H11": "Stopp: Regulatorische Bedenken", "LUDP": "Kurz-Pause: zu starke Schwankung (LULD)",
         "LUDS": "Kurz-Pause: Schwankung (LULD)", "MWC1": "Marktweiter Stopp Stufe 1", "MWC2": "Marktweiter Stopp Stufe 2",
         "MWC3": "Marktweiter Stopp Stufe 3", "M": "Kurs-Pause (Schwankung)", "D": "Aus dem Handel genommen",
         "IPO1": "Börsengang: Handel noch nicht gestartet", "R4": "Handel wieder aufgenommen", "C3": "Stopp: Ausgabe abgesagt"}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


K = _load(os.path.join(HERE, "kandidaten.py"), "trader_kandidaten_for_markt")
E = K.entry_mod()


class MarktError(Exception):
    pass


# ---------------------------------------------------------------- the weather

def quote(sym, rng="3mo"):
    """Daily closes (finished sessions), the last price and yesterday's close."""
    text = E.yahoo_get(E.chart_path(sym, rng, "1d"))
    ch = E.parse_chart(text, sym)
    meta = ch["meta"]
    today = E.to_eastern(E.utc_now()).date()
    closes = []
    for b in ch["bars"]:
        d = E.to_eastern(E.utc_of(b["t"])).date()
        if d < today:
            closes.append(b["c"])
    last = meta.get("regularMarketPrice")
    prev = closes[-1] if closes else meta.get("chartPreviousClose")
    return {"symbol": sym, "last": last, "prev": prev, "closes": closes,
            "pct": None if not (last and prev) else round((last / prev - 1) * 100, 2)}


def light(spy_last, spy_ma20, spy_pct, vix):
    if (vix is not None and vix >= 25) or (spy_pct is not None and spy_pct <= -1.0) or \
            (spy_ma20 and spy_last < spy_ma20 and (spy_pct or 0) < 0):
        return "rot"
    if spy_ma20 and spy_last > spy_ma20 and (spy_pct or 0) >= -0.2 and vix is not None and vix < 20:
        return "grün"
    return "gelb"


def status(get=quote):
    out = {"zeit": E.to_eastern(E.utc_now()).strftime("%Y-%m-%d %H:%M ET")}
    for sym in ("SPY", "QQQ"):
        q = get(sym)
        ma = sum(q["closes"][-20:]) / 20.0 if len(q["closes"]) >= 20 else None
        out[sym.lower()] = {"kurs": q["last"], "heute_pct": q["pct"], "ma20": round(ma, 2) if ma else None,
                            "ueber_ma20": None if ma is None or q["last"] is None else q["last"] > ma}
    v = get("^VIX")
    out["vix"] = {"stand": v["last"], "heute_pct": v["pct"]}
    sec = []
    for sym, name in SECTORS:
        try:
            q = get(sym, "5d")
            if q["pct"] is not None:
                sec.append({"etf": sym, "branche": name, "heute_pct": q["pct"]})
        except Exception:
            continue
    sec.sort(key=lambda s: s["heute_pct"], reverse=True)
    out["branchen"] = sec
    out["ampel"] = light(out["spy"]["kurs"], out["spy"]["ma20"], out["spy"]["heute_pct"], out["vix"]["stand"])
    out["grund"] = reason(out)
    return out


def reason(s):
    spy, vix = s["spy"], s["vix"]
    parts = ["SPY %s %% heute" % K.de(spy["heute_pct"]),
             "über" if spy["ueber_ma20"] else "unter",
             "dem 20-Tage-Schnitt",
             "VIX %s" % K.de(vix["stand"], 1)]
    return "%s, %s %s, %s" % tuple(parts)


# ---------------------------------------------------------------- the radar

def _sec_get(url):
    ua = os.environ.get("SEC_USER_AGENT") or ""
    news = _load(os.path.join(ROOT, "trader-news", "news.py"), "trader_news_for_markt")
    ua = news.sec_user_agent(None, "trader/config.json") or ua
    if not ua:
        raise MarktError("kein SEC User-Agent (sec_user_agent in trader/config.json oder SEC_USER_AGENT)")
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("latin-1")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (trader-kandidaten)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8-sig", "replace")


_TICKERS = {}


def ticker_of(cik, get=None):
    if not _TICKERS:
        data = json.loads((get or _sec_get)("https://www.sec.gov/files/company_tickers.json"))
        for v in data.values():
            _TICKERS.setdefault(int(v["cik_str"]), str(v["ticker"]).upper())
    return _TICKERS.get(int(cik))


def parse_8k_feed(text):
    """SEC 'getcurrent' Atom feed -> [{company, cik, time, items, link}]."""
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        m = re.match(r"\s*(8-K(?:/A)?)\s+-\s+(.*?)\s+\((\d{10})\)", html.unescape(t.group(1)) if t else "")
        if not m:
            continue
        summ = html.unescape((re.search(r"<summary[^>]*>(.*?)</summary>", e, re.S) or [None, ""])[1] or "")
        items = re.findall(r"Item (\d\.\d\d)", summ)
        link = (re.search(r'href="([^"]+)"', e) or [None, ""])[1]
        upd = (re.search(r"<updated>(.*?)</updated>", e) or [None, ""])[1]
        out.append({"form": m.group(1), "company": m.group(2).strip(), "cik": int(m.group(3)),
                    "time": upd, "items": items, "link": link})
    return out


def parse_halts(text, day=None):
    """Nasdaq trade-halts RSS -> [{symbol, name, time, code, meaning, resumed}] for one day."""
    out = []
    for it in re.findall(r"<item>(.*?)</item>", text, re.S):
        def g(tag):
            m = re.search(r"<ndaq:%s>(.*?)</ndaq:%s>" % (tag, tag), it, re.S)
            return html.unescape(m.group(1).strip()) if m else ""
        d = g("HaltDate")
        if day and d != day.strftime("%m/%d/%Y"):
            continue
        code = g("ReasonCode")
        out.append({"symbol": g("IssueSymbol"), "name": g("IssueName"), "time": g("HaltTime")[:8], "code": code,
                    "meaning": HALTS.get(code, "Handelsstopp (%s)" % code), "resumed": g("ResumptionTradeTime")[:8]})
    return out


def radar(minutes=180, tickers=None, strong_only=True, get8k=None, gethalts=None, getsec=None):
    now = E.utc_now()
    feed = parse_8k_feed((get8k or _sec_get)(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&count=100&output=atom"))
    want = {t.upper() for t in tickers} if tickers else None
    filings = []
    for f in feed:
        try:
            ts = datetime.datetime.fromisoformat(f["time"]).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
        if (now - ts).total_seconds() > minutes * 60:
            continue
        tk = ticker_of(f["cik"], getsec)
        if not tk or (want and tk not in want):
            continue
        items = [i for i in f["items"] if i in ITEMS]
        if strong_only and not (set(items) & STRONG):
            continue
        filings.append({"ticker": tk, "firma": f["company"], "zeit": E.to_eastern(ts).strftime("%H:%M"),
                        "was": ", ".join(ITEMS[i] for i in items) or "8-K", "items": items, "link": f["link"]})
    halts = parse_halts((gethalts or _get)("https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"),
                        E.to_eastern(now).date())
    if want:
        halts = [h for h in halts if h["symbol"] in want]
    halts = [h for h in halts if not h["symbol"].endswith(("W", "R", "U")) or len(h["symbol"]) <= 4]
    return {"zeit": E.to_eastern(now).strftime("%Y-%m-%d %H:%M ET"), "meldungen": filings, "stopps": halts}


# ---------------------------------------------------------------- dates

def upcoming(events, today, days=14):
    out = []
    for ev in events:
        try:
            d = datetime.date.fromisoformat(str(ev.get("datum"))[:10])
        except ValueError:
            continue
        if today <= d <= today + datetime.timedelta(days=days):
            out.append(dict(ev, in_tagen=(d - today).days))
    return sorted(out, key=lambda e: (e["datum"], e.get("zeit", "")))


# ---------------------------------------------------------------- commands

def main(argv=None):
    ap = argparse.ArgumentParser(description="Markt-Lage (trader-kandidaten). Standard library only.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("radar")
    p.add_argument("--minutes", type=int, default=180, help="Meldungen der letzten N Minuten (180)")
    p.add_argument("--tickers", nargs="*", help="nur diese Ticker")
    p.add_argument("--alle", action="store_true", help="auch weniger wichtige 8-K-Punkte")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("termine")
    p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "status":
            s = status()
            if a.json:
                print(json.dumps(s, indent=2, ensure_ascii=False))
                return 0
            print("Markt %s: Ampel %s (%s)" % (s["zeit"], s["ampel"].upper(), s["grund"]))
            print("QQQ %s %% heute, %s dem 20-Tage-Schnitt" % (K.de(s["qqq"]["heute_pct"]),
                                                              "über" if s["qqq"]["ueber_ma20"] else "unter"))
            if s["branchen"]:
                top, flop = s["branchen"][:3], s["branchen"][-3:]
                print("Stark: " + ", ".join("%s %s %%" % (b["branche"], K.de(b["heute_pct"])) for b in top))
                print("Schwach: " + ", ".join("%s %s %%" % (b["branche"], K.de(b["heute_pct"])) for b in flop))
            return 0
        if a.cmd == "radar":
            r = radar(a.minutes, a.tickers, not a.alle)
            if a.json:
                print(json.dumps(r, indent=2, ensure_ascii=False))
                return 0
            print("Radar %s, Meldungen der letzten %d Minuten:" % (r["zeit"], a.minutes))
            for f in r["meldungen"]:
                print("  %s  %-6s %s: %s" % (f["zeit"], f["ticker"], f["firma"][:40], f["was"]))
            if not r["meldungen"]:
                print("  keine wichtigen 8-K-Meldungen")
            print("Handelsstopps heute (Nasdaq-Liste):")
            for h in r["stopps"][:40]:
                print("  %s  %-6s %s%s" % (h["time"], h["symbol"], h["meaning"],
                                          (", weiter ab %s" % h["resumed"]) if h["resumed"] else ""))
            return 0
        with open(a.from_file, encoding="utf-8") as fh:
            ev = json.load(fh)
        up = upcoming(ev if isinstance(ev, list) else ev.get("termine", []), E.to_eastern(E.utc_now()).date(), a.days)
        if a.json:
            print(json.dumps(up, indent=2, ensure_ascii=False))
        else:
            for e in up:
                print("%s %s  %s (in %d Tagen)" % (e["datum"], e.get("zeit", ""), e.get("was", ""), e["in_tagen"]))
        return 0
    except (MarktError, K.KError, E.EntryError) as e:
        sys.stderr.write("markt.py: %s\n" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
