import datetime
import json
import unittest

from _load import load

M = load("trader-kandidaten", "markt")

FEED = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>8-K - NIKE, Inc. (0000320187) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/320187/x-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-11-09 &lt;br&gt;Item 5.02: Departure of Directors
&lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary>
<updated>2026-11-09T16:42:00-05:00</updated>
</entry>
<entry>
<title>8-K - Some Fund LLC (0009999999) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/x"/>
<summary type="html">&lt;br&gt;Item 2.02: Results of Operations</summary>
<updated>2026-11-09T16:40:00-05:00</updated>
</entry>
<entry>
<title>8-K - Quiet Corp (0000000002) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/y"/>
<summary type="html">&lt;br&gt;Item 9.01: Financial Statements and Exhibits</summary>
<updated>2026-11-09T16:41:00-05:00</updated>
</entry>
</feed>"""

HALTS = """<rss><channel>
<item><title>ABCD</title><ndaq:HaltDate>11/09/2026</ndaq:HaltDate><ndaq:HaltTime>10:04:18.073</ndaq:HaltTime>
<ndaq:IssueSymbol>ABCD</ndaq:IssueSymbol><ndaq:IssueName>Abcd Inc.</ndaq:IssueName><ndaq:ReasonCode>T1</ndaq:ReasonCode>
<ndaq:ResumptionTradeTime>10:40:00</ndaq:ResumptionTradeTime></item>
<item><title>OLD</title><ndaq:HaltDate>11/06/2026</ndaq:HaltDate><ndaq:HaltTime>11:00:00.000</ndaq:HaltTime>
<ndaq:IssueSymbol>OLD</ndaq:IssueSymbol><ndaq:IssueName>Old Inc.</ndaq:IssueName><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>
<ndaq:ResumptionTradeTime /></item>
</channel></rss>"""


class Light(unittest.TestCase):
    def test_rules(self):
        self.assertEqual(M.light(510, 500, 0.3, 15), "grün")
        self.assertEqual(M.light(510, 500, -0.5, 15), "gelb")       # down more than 0.2 %
        self.assertEqual(M.light(510, 500, 0.3, 21), "gelb")        # VIX 20+
        self.assertEqual(M.light(490, 500, -0.1, 15), "rot")        # under the average and down
        self.assertEqual(M.light(490, 500, 0.5, 15), "gelb")        # under the average but up today
        self.assertEqual(M.light(510, 500, -1.2, 15), "rot")        # a hard down day
        self.assertEqual(M.light(510, 500, 0.3, 26), "rot")         # fear


class Status(unittest.TestCase):
    def test_status_from_quotes(self):
        def get(sym, rng="3mo"):
            base = {"SPY": 500.0, "QQQ": 400.0, "^VIX": 15.0}.get(sym, 50.0)
            closes = [base] * 25
            return {"symbol": sym, "last": base * 1.01, "prev": base, "closes": closes, "pct": 1.0}
        s = M.status(get)
        self.assertEqual(s["ampel"], "grün")
        self.assertTrue(s["spy"]["ueber_ma20"])
        self.assertEqual(len(s["branchen"]), len(M.SECTORS))
        self.assertIn("VIX", s["grund"])


class Radar(unittest.TestCase):
    def test_feed_parsing(self):
        rows = M.parse_8k_feed(FEED)
        self.assertEqual([r["cik"] for r in rows], [320187, 9999999, 2])
        self.assertEqual(rows[0]["items"], ["5.02", "9.01"])
        self.assertEqual(rows[0]["company"], "NIKE, Inc.")

    def test_halts_today_only(self):
        h = M.parse_halts(HALTS, datetime.date(2026, 11, 9))
        self.assertEqual([x["symbol"] for x in h], ["ABCD"])
        self.assertEqual(h[0]["meaning"], "Stopp: Nachricht kommt gleich")
        self.assertEqual(h[0]["resumed"], "10:40:00")

    def test_radar_keeps_listed_companies_with_strong_items(self):
        M._TICKERS.clear()
        sec = lambda url: json.dumps({"0": {"cik_str": 320187, "ticker": "NKE"}, "1": {"cik_str": 2, "ticker": "QUI"}})
        orig = M.E.utc_now
        M.E.utc_now = lambda: datetime.datetime(2026, 11, 9, 22, 0)     # 17:00 ET
        try:
            r = M.radar(120, None, True, get8k=lambda u: FEED, gethalts=lambda u: HALTS, getsec=sec, extra=False)
        finally:
            M.E.utc_now = orig
            M._TICKERS.clear()
        self.assertEqual([f["ticker"] for f in r["meldungen"]], ["NKE"])   # fund has no ticker, QUI only 9.01
        self.assertEqual(r["meldungen"][0]["was"], "Wechsel im Management")
        self.assertEqual([h["symbol"] for h in r["stopps"]], ["ABCD"])


PRN = """<rss><channel>
<item><title>Rosen Law Firm Encourages NVE Corporation Investors to Inquire - NVEC</title><link>https://x/1</link>
<pubDate>Mon, 9 Nov 2026 22:37:00 +0000</pubDate><description>(NASDAQ: NVEC)</description></item>
<item><title>Solidion Technology (NASDAQ: STI) Reports Third Quarter Financial Results</title><link>https://x/2</link>
<pubDate>Mon, 9 Nov 2026 22:36:00 +0000</pubDate><description>DALLAS /PRNewswire/</description></item>
<item><title>Acme Corp to Acquire Widget Inc. (NYSE: WID)</title><link>https://x/3</link>
<pubDate>Mon, 9 Nov 2026 10:00:00 +0000</pubDate><description>old</description></item>
</channel></rss>"""

WSB = """<feed><entry><title>NVDA puts are printing, YOLO on $ASTS</title><updated>2026-11-09T21:00:00+00:00</updated></entry>
<entry><title>ASTS to the moon, DD inside</title><updated>2026-11-09T20:00:00+00:00</updated></entry>
<entry><title>Old post about TSLA</title><updated>2026-11-01T20:00:00+00:00</updated></entry></feed>"""


class Sources(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 9, 23, 0)

    def test_press_releases_skip_law_firms_and_old_items(self):
        out = M.press_releases(120, self.NOW, get=lambda u: PRN if "prnewswire" in u else "<rss></rss>")
        self.assertEqual([p["tickers"] for p in out], [["STI"]])
        self.assertEqual(out[0]["art"], ["Zahlen"])
        self.assertEqual(out[0]["zeit"], "17:36")

    def test_wsb_counts_known_tickers(self):
        st = json.dumps({"symbols": [{"symbol": "ASTS", "title": "AST SpaceMobile", "trending_score": 20.4}]})
        out = M.talked_about(self.NOW, known={"NVDA", "ASTS", "TSLA"},
                             get=lambda u: WSB, getjson=lambda u: st)
        self.assertEqual(out["stocktwits"][0]["ticker"], "ASTS")
        self.assertEqual(out["wsb"], [{"ticker": "ASTS", "beitraege": 2}, {"ticker": "NVDA", "beitraege": 1}])


class Tendency(unittest.TestCase):
    def test_tone(self):
        self.assertEqual(M.tone("Acme agrees to be acquired by Big Co for $40 per share in cash")[0], 3)
        self.assertEqual(M.tone("Zeta Reports Beat and Raise Quarter, Raises Full-Year Guidance")[0], 3)
        self.assertEqual(M.tone("TEN Holdings Announces Pricing of $2.0 Million Registered Direct Offering")[0], -2)
        self.assertEqual(M.tone("Revenue falls short of expectations")[0], -2)     # not also "Kurs fällt"
        self.assertEqual(M.tone("Energizer declares quarterly dividend")[0], 0)

    def test_headline_tickers(self):
        self.assertEqual(M.headline_tickers("As Nvidia's stock falls, AMD gains"), {"NVDA", "AMD"})
        self.assertEqual(M.headline_tickers("Solidion (NASDAQ: STI) reports"), {"STI"})
        self.assertEqual(M.headline_tickers("Target practice for the Fed"), set())

    def test_affected_combines_sources(self):
        r = {"meldungen": [{"ticker": "STI", "firma": "Solidion", "zeit": "17:30", "was": "Quartalszahlen",
                            "items": ["2.02"], "link": "s"},
                           {"ticker": "BAD", "firma": "Bad Inc", "zeit": "17:00", "was": "Insolvenz", "items": ["1.03"], "link": ""}],
             "stopps": [{"symbol": "HLT", "code": "T1", "time": "10:04:18", "meaning": "Stopp"}],
             "pressemitteilungen": [{"zeit": "17:36", "tickers": ["STI"], "titel": "Solidion beats estimates",
                                     "art": ["Zahlen"], "quelle": "PR Newswire", "link": "p"}],
             "schlagzeilen": [{"zeit": "09.11. 16:55", "titel": "Nvidia stock falls on probe", "quelle": "MarketWatch", "link": "m"}]}
        rows = {x["ticker"]: x for x in M.affected(r)}
        self.assertEqual(rows["BAD"]["tendenz"], -3)
        self.assertEqual(rows["STI"]["tendenz"], 2)
        self.assertEqual(len(rows["STI"]["meldungen"]), 2)
        self.assertEqual(rows["NVDA"]["tendenz"], -2)
        self.assertTrue(rows["HLT"]["offen"])
        self.assertEqual(M.affected(r)[0]["ticker"], "BAD")


class Dates(unittest.TestCase):
    def test_upcoming(self):
        ev = [{"datum": "2026-12-09", "zeit": "20:00", "was": "Fed-Zinsentscheid"},
              {"datum": "2026-11-13", "zeit": "14:30", "was": "Inflation (CPI)"},
              {"datum": "2026-11-01", "was": "vorbei"}]
        up = M.upcoming(ev, datetime.date(2026, 11, 9), 14)
        self.assertEqual([e["was"] for e in up], ["Inflation (CPI)"])
        self.assertEqual(up[0]["in_tagen"], 4)


if __name__ == "__main__":
    unittest.main()
