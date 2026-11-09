#!/usr/bin/env python3
"""Markt-Lage (trader-kandidaten): what the whole market is doing, and what just broke.

  markt.py status            the weather: SPY and QQQ against their 20-day average,
                             today's move, the VIX, which sectors lead and lag,
                             and one light: gruen / gelb / rot
  markt.py radar             fresh SEC 8-K filings of listed companies that move
                             stocks (earnings, deals, CEO changes, bankruptcy ...),
                             today's Nasdaq trading halts, company press releases
                             (Business Wire, PR Newswire), market headlines
                             (MarketWatch, Google News), what is being talked
                             about (StockTwits trending, r/wallstreetbets) and the
                             Fed's press releases
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


def radar(minutes=180, tickers=None, strong_only=True, get8k=None, gethalts=None, getsec=None, extra=True):
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
    out = {"zeit": E.to_eastern(now).strftime("%Y-%m-%d %H:%M ET"), "meldungen": filings, "stopps": halts}
    if extra:
        known = set(_TICKERS.values()) or None
        out["pressemitteilungen"] = press_releases(minutes, now)
        if want:
            out["pressemitteilungen"] = [p for p in out["pressemitteilungen"] if set(p["tickers"]) & want]
        out["schlagzeilen"] = headlines(max(minutes, 12 * 60), now)
        out["im_gespraech"] = talked_about(now, known)
        out["notenbank"] = fed_news(3, now)
    out["betroffen"] = affected(out)
    return out


# ---------------------------------------------------------------- more sources

BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
FEEDS = {
    "businesswire": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeGVtRWA==",
    "prnewswire": "https://www.prnewswire.com/rss/news-releases-list.rss",
    "marketwatch": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "googlenews": "https://news.google.com/rss/search?q=stock+market+today&hl=en-US&gl=US&ceid=US:en",
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "wsb": "https://www.reddit.com/r/wallstreetbets/.rss",
    "stocktwits": "https://api.stocktwits.com/api/2/trending/symbols.json",
}
EXCHANGE_TICKER = re.compile(r"\((?:NASDAQ|Nasdaq|NYSE(?: American| Arca)?|Cboe|OTCQX|OTCQB|TSX)\s*:\s*([A-Z][A-Z.]{0,5})\)")
SPAM = re.compile(r"law firm|class action|investor alert|shareholder alert|investigation|lawsuit|deadline|"
                  r"rosen|levi & korsinsky|pomerantz|bronstein|faruqi|schall|glancy|kessler topaz|bragar", re.I)
KINDS = [("Zahlen", re.compile(r"\bresults\b|reports? (first|second|third|fourth|q[1-4]|fiscal|quarter)|earnings", re.I)),
         ("Übernahme", re.compile(r"acqui|merger|to be acquired|definitive agreement|tender offer", re.I)),
         ("FDA", re.compile(r"\bFDA\b|approval|clinical|phase [123]", re.I)),
         ("Ausblick", re.compile(r"guidance|outlook|raises|lowers|forecast", re.I)),
         ("Kapital", re.compile(r"offering|private placement|buyback|repurchase|dividend", re.I))]
NOT_TICKERS = set("A I AI ALL AM AN AND ARE AS AT ATH BE BIG BUY BY CEO CFO CPI DD DOW EOD EPS ETF FBI FDA FED FOMC FOR "
                  "GDP GO HAS HE HOLD I IF IN IPO IS IT ITM LOL MY NEW NO NOT NOW OF ON ONE OR OTM OUT PM PUT PUTS SEC "
                  "SO THE TO UP US USA WE WSB YOLO YOU CALL CALLS RIP OP IMO TL DR TLDR GDP".split())


def rss_items(text):
    """RSS or Atom -> [{title, link, time (naive UTC or None), text}]."""
    out = []
    for b in re.findall(r"<(?:item|entry)\b(.*?)</(?:item|entry)>", text, re.S):
        def g(pat):
            m = re.search(pat, b, re.S)
            if not m:
                return ""
            v = re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))
            return html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(v))).strip()
        link = re.search(r"<link[^>]*href=\"([^\"]+)\"", b) or re.search(r"<link>(.*?)</link>", b, re.S)
        when = g(r"<(?:pubDate|updated|published)>(.*?)</")
        out.append({"title": " ".join(g(r"<title[^>]*>(.*?)</title>").split()), "link": html.unescape(link.group(1).strip()) if link else "",
                    "time": _when(when), "text": " ".join(g(r"<(?:description|summary|content)[^>]*>(.*?)</").split())[:400]})
    return out


def _when(s):
    import email.utils
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        return d
    return d.astimezone(datetime.timezone.utc).replace(tzinfo=None)


def _fresh(items, minutes, now):
    return [i for i in items if i["time"] and (now - i["time"]).total_seconds() <= minutes * 60]


def _kind(text):
    return [k for k, rx in KINDS if rx.search(text)]


def press_releases(minutes, now, get=None):
    """Company press releases with a ticker, without the law-firm advertising."""
    get = get or (lambda u: _get_as(u, BROWSER))
    out = []
    for src, name in (("businesswire", "Business Wire"), ("prnewswire", "PR Newswire")):
        try:
            items = rss_items(get(FEEDS[src]))
        except Exception:
            continue
        for i in _fresh(items, minutes, now):
            blob = i["title"] + " " + i["text"]
            if SPAM.search(i["title"]):
                continue
            tks = sorted(set(EXCHANGE_TICKER.findall(blob)) |
                         set(t.upper() for t in re.findall(r"BUSINESS WIRE\)-+\s*((?:\$[a-z.]{1,6}\s*)+)", blob) for t in re.findall(r"[a-z.]+", t)))
            if not tks:
                continue
            out.append({"zeit": E.to_eastern(i["time"]).strftime("%H:%M"), "tickers": tks, "titel": i["title"][:160],
                        "art": _kind(blob), "quelle": name, "link": i["link"]})
    out.sort(key=lambda x: x["zeit"], reverse=True)
    return out


def headlines(minutes, now, get=None):
    get = get or (lambda u: _get_as(u, BROWSER))
    out = []
    for src, name in (("marketwatch", "MarketWatch"), ("googlenews", "Google News")):
        try:
            items = rss_items(get(FEEDS[src]))
        except Exception:
            continue
        for i in _fresh(items, minutes, now)[:10]:
            out.append({"zeit": E.to_eastern(i["time"]).strftime("%d.%m. %H:%M"), "titel": i["title"][:180],
                        "quelle": name, "link": i["link"]})
    return out


def talked_about(now, known=None, get=None, getjson=None):
    """StockTwits trending and the tickers r/wallstreetbets titles name most in the last 24 hours."""
    get = get or (lambda u: _get_as(u, BROWSER))
    out = {"stocktwits": [], "wsb": []}
    try:
        data = json.loads((getjson or get)(FEEDS["stocktwits"]))
        for s_ in (data.get("symbols") or [])[:15]:
            out["stocktwits"].append({"ticker": s_.get("symbol"), "name": s_.get("title"),
                                      "score": round(float(s_.get("trending_score") or 0), 1)})
    except Exception:
        pass
    try:
        items = _fresh(rss_items(get(FEEDS["wsb"])), 24 * 60, now)
        counts = {}
        for i in items:
            words = set(re.findall(r"\$([A-Z]{1,5})\b", i["title"])) | set(re.findall(r"\b([A-Z]{2,5})\b", i["title"]))
            for w in words:
                if w in NOT_TICKERS or (known is not None and w not in known):
                    continue
                counts[w] = counts.get(w, 0) + 1
        out["wsb"] = [{"ticker": t, "beitraege": n} for t, n in sorted(counts.items(), key=lambda x: -x[1])[:10]]
    except Exception:
        pass
    return out


def fed_news(days, now, get=None):
    get = get or (lambda u: _get_as(u, BROWSER))
    try:
        items = rss_items(get(FEEDS["fed"]))
    except Exception:
        return []
    return [{"zeit": E.to_eastern(i["time"]).strftime("%d.%m. %H:%M"), "titel": i["title"][:180], "link": i["link"]}
            for i in _fresh(items, days * 24 * 60, now)[:5]]


# ---------------------------------------------------------------- tendency

# Keyword rules for the likely direction of a news item, -3 (strongly down) .. +3 (strongly up).
# Order matters only for the reason text; all matching rules add up, the sum is clipped to -3..+3.
TONE = [
    (+3, "wird übernommen", re.compile(r"to be acquired|agrees? to be acquired|acquired by|take[- ]private|"
                                       r"per share in cash|all-cash (deal|transaction)", re.I)),
    (+2, "Zahlen besser als erwartet", re.compile(r"\bbeats?\b|tops? (estimates|expectations)|above (estimates|expectations)|"
                                                   r"record (revenue|quarter|results)|beat and raise", re.I)),
    (+2, "Ausblick angehoben", re.compile(r"raises? (its |full[- ]year |annual )?(guidance|outlook|forecast)|raised (guidance|outlook)", re.I)),
    (+2, "Zulassung", re.compile(r"\bFDA (approv|clear)|approved by the FDA|receives? (FDA )?approval|granted approval", re.I)),
    (+1, "Aktienrückkauf", re.compile(r"buyback|repurchase (program|authorization)", re.I)),
    (+1, "Dividende erhöht", re.compile(r"(raises|increases|boosts) (its )?(quarterly )?dividend|dividend increase", re.I)),
    (+1, "Auftrag / Partnerschaft", re.compile(r"\b(awarded|wins|secures|signs)\b.*\b(contract|agreement|order|deal)\b|partnership with|multi-year (agreement|contract|deal)", re.I)),
    (+1, "Hochstufung", re.compile(r"\bupgrade[sd]?\b", re.I)),
    (+1, "Kurs steigt", re.compile(r"\b(surges?|soars?|jumps?|rall(y|ies)|climbs?|spikes?)\b", re.I)),
    (-1, "Kurs fällt", re.compile(r"\b(falls?(?! short)|drops?|slides?|slumps?|tumbles?|sinks?|plunges?|stock falls)\b", re.I)),
    (-1, "Herabstufung", re.compile(r"\bdowngrade[sd]?\b", re.I)),
    (-1, "Chef geht", re.compile(r"\b(resigns?|steps? down|departure of|to depart)\b", re.I)),
    (-1, "Skandal / Rückruf / Ermittlung", re.compile(r"\brecall\b|subpoena|\bprobe\b|investigation by|scandal", re.I)),
    (-2, "Zahlen schwächer als erwartet", re.compile(r"\bmiss(es|ed)?\b (estimates|expectations)|falls? short|below (estimates|expectations)", re.I)),
    (-2, "Ausblick gesenkt", re.compile(r"(lowers|cuts|reduces|slashes) (its |full[- ]year |annual )?(guidance|outlook|forecast)", re.I)),
    (-2, "neue Aktien (Verwässerung)", re.compile(r"(public|registered direct|underwritten|secondary) offering|private placement|"
                                                  r"at-the-market|pricing of .*offering|proposed offering", re.I)),
    (-2, "Börsennotiz gefährdet", re.compile(r"delist|non-?compliance|deficiency (notice|letter)|minimum bid", re.I)),
    (-3, "Insolvenz", re.compile(r"bankruptcy|chapter 11|going concern", re.I)),
    (-3, "Bilanz unzuverlässig", re.compile(r"restate|non-reliance", re.I)),
    (-3, "Studie gescheitert", re.compile(r"(trial|study) (failed|did not meet|missed)|failed (trial|study)|"
                                          r"discontinu(e|es|ed) (development|the program)|complete response letter", re.I)),
]
ITEM_TONE = {"1.03": (-3, "Insolvenz (8-K)"), "4.02": (-3, "Bilanz unzuverlässig (8-K)"), "3.01": (-2, "Börsennotiz gefährdet (8-K)"),
             "2.06": (-2, "Wertberichtigung (8-K)"), "1.02": (-1, "Vertrag beendet (8-K)"), "2.05": (-1, "Stellenabbau (8-K)"),
             "3.02": (-1, "neue Aktien (8-K)"), "2.02": (0, "Quartalszahlen, Richtung offen"),
             "5.02": (0, "Wechsel im Management, Richtung offen"), "1.01": (0, "Vertrag / Deal, Richtung offen"),
             "2.01": (0, "Übernahme / Verkauf abgeschlossen"), "5.01": (0, "Kontrollwechsel, Richtung offen")}
HALT_TONE = {"H10": (-3, "SEC hat den Handel ausgesetzt"), "H11": (-2, "Regulatorische Bedenken"), "T12": (-1, "Börse will mehr Infos"),
             "T1": (0, "Handelsstopp: Nachricht kommt"), "T2": (0, "Handelsstopp: Nachricht raus")}
# Big names that headlines write without a ticker.
ALIASES = {"nvidia": "NVDA", "apple": "AAPL", "microsoft": "MSFT", "amazon": "AMZN", "alphabet": "GOOGL", "google": "GOOGL",
           "meta platforms": "META", "facebook": "META", "tesla": "TSLA", "netflix": "NFLX", "amd": "AMD", "broadcom": "AVGO",
           "intel": "INTC", "qualcomm": "QCOM", "micron": "MU", "oracle": "ORCL", "salesforce": "CRM", "palantir": "PLTR",
           "boeing": "BA", "walmart": "WMT", "costco": "COST", "disney": "DIS", "nike": "NKE", "pfizer": "PFE",
           "moderna": "MRNA", "eli lilly": "LLY", "novo nordisk": "NVO", "jpmorgan": "JPM", "goldman sachs": "GS",
           "bank of america": "BAC", "berkshire": "BRK.B", "exxon": "XOM", "chevron": "CVX", "uber": "UBER", "coinbase": "COIN",
           "super micro": "SMCI", "arm holdings": "ARM", "taiwan semiconductor": "TSM", "tsmc": "TSM", "starbucks": "SBUX",
           "mcdonald's": "MCD", "home depot": "HD", "unitedhealth": "UNH", "visa": "V", "mastercard": "MA", "paypal": "PYPL",
           "shopify": "SHOP", "rivian": "RIVN", "lucid": "LCID", "ford": "F", "general motors": "GM", "ibm": "IBM", "cisco": "CSCO",
           "adobe": "ADBE", "snowflake": "SNOW", "spotify": "SPOT", "airbnb": "ABNB", "robinhood": "HOOD"}


def tone(text):
    """Keyword score of one news text -> (score -3..3, [reasons])."""
    score, why = 0, []
    for pts, label, rx in TONE:
        if rx.search(text):
            score += pts
            why.append(label)
    return max(-3, min(3, score)), why


def headline_tickers(text):
    found = set(EXCHANGE_TICKER.findall(text)) | set(re.findall(r"\$([A-Z]{1,5})\b", text))
    low = text.lower()
    for name, tk in ALIASES.items():
        if tk and re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", low):
            found.add(tk)
    return found


def affected(r):
    """All stocks named by the news in a radar result, one row each with a likely direction.

    -> [{ticker, tendenz -3..3, offen (direction unknown), gruende, meldungen: [{zeit, quelle, titel, link}]}],
    strongest direction first. A guess from keywords, not a forecast."""
    rows = {}

    def add(tk, pts, why, zeit, quelle, titel, link, offen=False):
        row = rows.setdefault(tk, {"ticker": tk, "summe": 0, "gruende": [], "meldungen": [], "offen": True})
        row["summe"] += pts
        for w in why:
            if w not in row["gruende"]:
                row["gruende"].append(w)
        if not offen:
            row["offen"] = False
        row["meldungen"].append({"zeit": zeit, "quelle": quelle, "titel": titel, "link": link or ""})

    for f in r.get("meldungen", []):
        pts, why, open_ = 0, [], True
        for i in f.get("items", []):
            p, w = ITEM_TONE.get(i, (0, None))
            if w:
                pts += p
                why.append(w)
                open_ = open_ and p == 0
        add(f["ticker"], pts, why, f["zeit"], "SEC 8-K", "%s: %s" % (f.get("firma", ""), f["was"]), f.get("link"), open_)
    for h in r.get("stopps", []):
        if h.get("code") in HALT_TONE:
            p, w = HALT_TONE[h["code"]]
            add(h["symbol"], p, [w], h.get("time", "")[:5], "Nasdaq", h.get("meaning", ""), "", p == 0)
    for p in r.get("pressemitteilungen", []):
        pts, why = tone(p["titel"])
        if "Übernahme" in p.get("art", []) and not why:
            why = ["Übernahme, Richtung offen (Käufer oder Ziel?)"]
        for tk in p["tickers"]:
            add(tk, pts, why, p["zeit"], p["quelle"], p["titel"], p.get("link"), pts == 0)
    for h in r.get("schlagzeilen", []):
        tks = headline_tickers(h["titel"])
        if not tks:
            continue
        pts, why = tone(h["titel"])
        if len(tks) > 1:        # which company the words belong to is unclear
            pts, why = 0, ["in einer Schlagzeile mit anderen Firmen genannt"]
        for tk in tks:
            add(tk, pts, why, h["zeit"], h["quelle"], h["titel"], h.get("link"), pts == 0)
    out = []
    for row in rows.values():
        row["tendenz"] = max(-3, min(3, row.pop("summe")))
        if row["tendenz"] != 0:
            row["offen"] = False
        if not row["gruende"]:
            row["gruende"] = ["keine eindeutigen Stichworte"]
        out.append(row)
    out.sort(key=lambda x: (-abs(x["tendenz"]), x["offen"], x["ticker"]))
    return out


def _get_as(url, ua):
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8-sig", "replace")


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
    p.add_argument("--nur-sec", action="store_true", help="nur SEC und Handelsstopps, ohne die übrigen Quellen")
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
            r = radar(a.minutes, a.tickers, not a.alle, extra=not a.nur_sec)
            if a.json:
                print(json.dumps(r, indent=2, ensure_ascii=False))
                return 0
            print("Radar %s, Meldungen der letzten %d Minuten:" % (r["zeit"], a.minutes))
            for f in r["meldungen"]:
                print("  %s  %-6s %s: %s" % (f["zeit"], f["ticker"], f["firma"][:40], f["was"]))
            if not r["meldungen"]:
                print("  keine wichtigen 8-K-Meldungen")
            for p in r.get("pressemitteilungen", [])[:15]:
                print("  %s  %-12s %s%s" % (p["zeit"], ",".join(p["tickers"])[:12], p["titel"][:90],
                                            (" [%s]" % ", ".join(p["art"])) if p["art"] else ""))
            for h in r.get("schlagzeilen", [])[:8]:
                print("  %s  %s: %s" % (h["zeit"], h["quelle"], h["titel"][:100]))
            ig = r.get("im_gespraech") or {}
            if ig.get("stocktwits"):
                print("StockTwits gerade: " + ", ".join(s["ticker"] for s in ig["stocktwits"][:10]))
            if ig.get("wsb"):
                print("r/wallstreetbets (24 h): " + ", ".join("%s (%d)" % (w["ticker"], w["beitraege"]) for w in ig["wsb"]))
            for f in r.get("notenbank", []):
                print("  Fed %s: %s" % (f["zeit"], f["titel"][:100]))
            if r.get("betroffen"):
                print("Betroffene Aktien, Tendenz nach Stichworten (keine Prognose):")
                for b in r["betroffen"][:20]:
                    arrow = ("↑" * b["tendenz"]) if b["tendenz"] > 0 else ("↓" * -b["tendenz"]) if b["tendenz"] < 0 else "→"
                    print("  %-6s %-4s %s" % (b["ticker"], arrow, "; ".join(b["gruende"])[:100]))
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
