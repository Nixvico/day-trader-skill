import csv
import datetime
import json
import os
import tempfile
import unittest

from _load import load

K = load("trader-kandidaten", "kandidaten")
CFG = dict(K.DEFAULTS)
WHOLE = dict(CFG, fractional=False)


def row(**kw):
    r = {"ticker": "TEST", "status": "triggered", "earnings": "nein", "above_high": True,
         "target_pct": 3.0, "hurdle_r": 0.3, "sim_r": 0.4, "hist_r": 0.5, "age_min": 5}
    r.update(kw)
    return r


class Money(unittest.TestCase):
    def test_size_and_hurdle_for_500_eur(self):
        s = K.size_trade(13.89, 13.72, 14.23, WHOLE)     # RIVN 2026-11-09, whole shares
        self.assertEqual(s["shares"], 41)                # 575 $ / 13.89
        self.assertAlmostEqual(s["risk_eur"], 6.06, places=2)
        self.assertEqual(s["fees_eur"], 2.0)
        self.assertAlmostEqual(s["hurdle_r"], 0.43, places=2)
        self.assertAlmostEqual(s["loss_eur"], -8.06, places=2)
        self.assertAlmostEqual(s["target_pct"], 2.45, places=2)

    def test_fractional_shares_use_the_whole_budget(self):
        s = K.size_trade(13.89, 13.72, 14.23, CFG)       # fractions are the default
        self.assertAlmostEqual(s["shares"], 41.396, places=3)   # 575 / 13.89 = 41.3967, rounded down
        big = K.size_trade(900.0, 890.0, 920.0, CFG)     # a 900 dollar stock still fits
        self.assertAlmostEqual(big["shares"], 0.638, places=3)
        self.assertAlmostEqual(big["risk_eur"], 0.638 * 10 / 1.15, places=2)
        self.assertEqual(K.shares_text(41.397), "41,397")
        self.assertEqual(K.shares_text(6), "6")

    def test_no_price_cap_by_default(self):
        hits = [{"symbol": "BIG", "price": 900, "at": "10:10 AM"}]
        fresh, old = K.fresh_window(hits, K.minutes_of("10:15"), CFG, K.minutes_of("09:45"), CFG["max_price_usd"])
        self.assertEqual([h["symbol"] for h in fresh], ["BIG"])
        self.assertIsNone(K.read_config(None)["max_price_usd"])

    def test_more_budget_lowers_the_hurdle_toward_the_buffer(self):
        big = dict(CFG, budget_eur=50000)
        s = K.size_trade(13.89, 13.72, 14.23, big)
        self.assertLess(s["hurdle_r"], 0.11)
        self.assertGreaterEqual(s["hurdle_r"], CFG["fee_buffer_r"])

    def test_budget_too_small_for_one_share(self):
        s = K.size_trade(900.0, 890.0, 920.0, WHOLE)
        self.assertEqual(s["shares"], 0)
        self.assertIsNone(s["hurdle_r"])
        self.assertEqual(K.verdict(row(hurdle_r=None, **{"target_pct": 3.0}), CFG, False),
                         "raus (Budget zu klein für 1 Aktie)")


class Verdict(unittest.TestCase):
    def test_all_rules_pass(self):
        self.assertEqual(K.verdict(row(), CFG, True), "Test-Kandidat")

    def test_each_rule_on_its_own(self):
        cases = [
            (dict(status="waiting", phase="open"), "offen (noch kein Ausbruch)"),
            (dict(status="waiting", phase="forming"), "offen (Range ab 9:45)"),
            (dict(age_min=12), "raus (zu alt)"),
            (dict(above_high=False), "raus (unter Range-Hoch)"),
            (dict(target_pct=2.1), "raus (Ziel < 2,2 %)"),
            (dict(earnings="ja"), "raus (Zahlen heute/gestern)"),
            (dict(earnings="steht an"), "raus (Zahlen stehen an)"),
            (dict(earnings="?"), "raus (News nicht abrufbar)"),
            (dict(sim_r=0.29), "raus (zu schwach nach Gebühren)"),
            (dict(hist_r=0.1), "raus (zu schwach nach Gebühren)"),
            (dict(sim_r=None), "raus (Zeitmaschine ohne Ergebnis)"),
            (dict(error="boom"), "raus (Fehler: boom)"),
        ]
        for kw, want in cases:
            self.assertEqual(K.verdict(row(**kw), CFG, True), want, kw)

    def test_freshness_only_counts_in_scan_mode(self):
        self.assertEqual(K.verdict(row(age_min=40), CFG, False), "Test-Kandidat")


class Earnings(unittest.TestCase):
    def item(self, title, tags=("earnings",), ticker="TTD", **kw):
        d = {"kind": "headline", "ticker": ticker, "touches": [ticker], "title": title, "tags": list(tags)}
        d.update(kw)
        return d

    def test_coming_up(self):
        it = self.item("The Trade Desk Jumps 8% Ahead of Earnings; Magnite Gains 2%")
        self.assertEqual(K.earnings_flag("TTD", [it], "The Trade Desk, Inc. - Class A")[0], "steht an")

    def test_reported(self):
        it = self.item("BCE (BCE) Surpasses Q3 Earnings and Revenue Estimates", ticker="BCE")
        self.assertEqual(K.earnings_flag("BCE", [it])[0], "ja")

    def test_filing_item_2_02(self):
        it = {"kind": "filing", "ticker": "DKNG", "touches": ["DKNG"], "title": "8-K", "items": ["2.02"], "tags": []}
        self.assertEqual(K.earnings_flag("DKNG", [it])[0], "ja")

    def test_other_companies_and_opinion_pieces_do_not_count(self):
        other = self.item("What Lexicon Pharmaceuticals (LXRX) Said on Its Q3 Earnings Call", ticker="NVO")
        opinion = self.item("Is Shell (SHEL) Stock Undervalued Right Now?", ticker="SHEL")
        untagged = self.item("PFE Q3 results", tags=(), ticker="PFE")
        self.assertEqual(K.earnings_flag("NVO", [other], "Novo Nordisk A/S")[0], "nein")
        self.assertEqual(K.earnings_flag("SHEL", [opinion], "Shell plc")[0], "nein")
        self.assertEqual(K.earnings_flag("PFE", [untagged], "Pfizer, Inc.")[0], "nein")


class Window(unittest.TestCase):
    def test_fresh_old_and_price_cap(self):
        hits = [{"symbol": "A", "price": 20, "at": "10:15 AM"}, {"symbol": "B", "price": 50, "at": "10:05 AM"},
                {"symbol": "C", "price": 150, "at": "10:15 AM"}, {"symbol": "D", "price": 10, "at": "9:50 AM"},
                {"symbol": "E", "price": 10, "at": "9:40 AM"}]
        fresh, old = K.fresh_window(hits, K.minutes_of("2026-11-09 10:15 ET"), CFG, K.minutes_of("09:45"), 100)
        self.assertEqual([h["symbol"] for h in fresh], ["A", "B"])
        self.assertEqual([h["symbol"] for h in old], ["D"])

    def test_minutes_of(self):
        self.assertEqual(K.minutes_of("12:05 PM"), 12 * 60 + 5)
        self.assertEqual(K.minutes_of("1:30 PM"), 13 * 60 + 30)
        self.assertEqual(K.minutes_of("2026-11-09 10:15 ET"), 615)


class Journal(unittest.TestCase):
    def bars(self, spec):
        return [{"m": K.minutes_of(t), "h": h, "l": lo, "c": c} for t, h, lo, c in spec]

    def test_stop_target_and_flat(self):
        b = self.bars([("10:00", 10.2, 10.0, 10.1), ("10:05", 10.6, 10.1, 10.5)])
        self.assertEqual(K.outcome(b, 600, 10.0, 9.8, 10.4, 955)[:2], ("Ziel", 10.4))
        b = self.bars([("10:00", 10.1, 9.7, 9.9)])
        self.assertEqual(K.outcome(b, 600, 10.0, 9.8, 10.4, 955)[:2], ("Stop", 9.8))
        both = self.bars([("10:00", 10.5, 9.7, 10.0)])
        self.assertEqual(K.outcome(both, 600, 10.0, 9.8, 10.4, 955)[0], "Stop")   # cautious reading
        day = self.bars([("%d:%02d" % divmod(m, 60), 10.1, 9.9, 10.05) for m in range(600, 960, 5)])
        self.assertEqual(K.outcome(day, 600, 10.0, 9.8, 10.4, 955), ("Schluss 15:55", 10.05, 955))
        half = self.bars([("10:00", 10.1, 9.9, 10.0)])
        self.assertIsNone(K.outcome(half, 600, 10.0, 9.8, 10.4, 955))      # day not done

    def test_add_update_summary_and_export(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tb.csv")
            r = row(entry=10.0, stop=9.8, target=10.4, entry_time="10:00 AM", shares=57, risk_eur=9.91,
                    fees_eur=2.0, now="2026-11-05 10:20:00", verdict="Test-Kandidat")
            self.assertEqual(K.journal_add(path, [r, dict(r, ticker="NOPE", verdict="raus (zu alt)")]), 1)
            self.assertEqual(K.journal_add(path, [r]), 0)                   # no duplicates
            bars = [{"t": 0, "h": 10.1, "l": 9.95, "c": 10.0}]
            em = K.entry_mod()
            base = em.eastern_to_utc(datetime.datetime(2026, 11, 5, 10, 0))
            chart = {"bars": [{"t": em.ts_of(base) + i * 300, "h": 10.5 if i == 2 else 10.1, "l": 9.95, "c": 10.0}
                              for i in range(72)]}
            n, rows = K.journal_update(path, CFG, now_text="2026-11-06 12:00", provider=lambda s, day: chart)
            self.assertEqual(n, 1)
            self.assertEqual(rows[0]["ergebnis"], "Ziel")
            self.assertEqual(rows[0]["ausstieg_zeit"], "10:10")
            self.assertEqual(rows[0]["r"], "2.00")
            self.assertEqual(rows[0]["gewinn_eur"], "%.2f" % (57 * 0.4 / CFG["eur_usd"] - 2.0))
            s = K.journal_summary(K.journal_read(path))
            self.assertEqual((s["trades"], s["closed"], s["wins"]), (1, 1, 1))
            out = os.path.join(d, "n.csv")
            K.journal_export(K.journal_read(path), out)
            with open(out, encoding="utf-8-sig") as fh:
                lines = list(csv.reader(fh, delimiter=";"))
            self.assertEqual(lines[0], K.JOURNAL_FIELDS)
            self.assertEqual(lines[1][3], "10,00")                          # decimal comma
            del bars

    def test_dump_and_import_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tb.csv")
            r = row(entry=10.0, stop=9.8, target=10.4, entry_time="10:00 AM", shares=57, risk_eur=9.91,
                    fees_eur=2.0, now="2026-11-05 10:20:00", verdict="Test-Kandidat")
            K.journal_add(path, [r])
            docs = K.journal_docs(K.journal_read(path))
            self.assertEqual(list(docs), ["2026-11-05_TEST"])
            doc = docs["2026-11-05_TEST"]
            self.assertEqual((doc["einstieg"], doc["stueck"], doc["ergebnis"], doc["gewinn_eur"]), (10.0, 57, "", None))
            other = os.path.join(d, "other.csv")
            done = dict(doc, ergebnis="Ziel", ausstieg=10.4, ausstieg_zeit="10:10", r=2.0, gewinn_eur=17.83)
            self.assertEqual(K.journal_import(other, [done]), 1)
            back = K.journal_read(other)[0]
            self.assertEqual((back["ergebnis"], back["ausstieg"], back["stueck"], back["gewinn_eur"]),
                             ("Ziel", "10.40", "57", "17.83"))

    def test_nothing_is_written_into_the_repo_by_default(self):
        self.assertTrue(K.DEFAULTS["workdir"].startswith("/tmp/"))
        self.assertTrue(K.DEFAULTS["journal"].startswith("trader/"))       # trader/ is git-ignored
        with open(os.path.join(K.ROOT, ".gitignore")) as fh:
            self.assertIn("trader/", fh.read().split())

    def test_example_config_matches_the_defaults(self):
        with open(os.path.join(K.HERE, "config.example.json")) as fh:
            ex = json.load(fh)
        self.assertEqual(sorted(ex), sorted(K.DEFAULTS))
        cfg = K.read_config(os.path.join(K.HERE, "config.example.json"))
        self.assertEqual(cfg["budget_eur"], 500.0)


if __name__ == "__main__":
    unittest.main()
