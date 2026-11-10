import datetime
import json
import unittest

from _load import load

S = load("trader-kandidaten", "swing")
CFG = dict(S.K.DEFAULTS)
D0 = datetime.date(2026, 1, 5)


def bars_from(rows):
    """rows of (o, h, l, c) -> bars on consecutive weekdays."""
    out, d = [], D0
    for o, h, l, c in rows:
        while d.weekday() >= 5:
            d += datetime.timedelta(days=1)
        out.append({"d": d, "o": o, "h": h, "l": l, "c": c})
        d += datetime.timedelta(days=1)
    return out


def flat(n, px=100.0, rng=2.0):
    return [(px, px + rng / 2, px - rng / 2, px) for _ in range(n)]


class Rule(unittest.TestCase):
    def test_signal_is_a_close_above_the_last_20_closes(self):
        b = bars_from(flat(25) + [(100, 103, 99, 102.5)])
        self.assertTrue(S.is_signal(b, 25))
        self.assertFalse(S.is_signal(b, 24))

    def test_plan_levels(self):
        b = bars_from(flat(25) + [(100, 103, 99, 102.5)])
        p = S.plan_from(b, 25)
        self.assertEqual(p["level"], 103.01)
        self.assertAlmostEqual(p["stop"], 103.01 - 1.5 * p["atr"], places=1)
        self.assertAlmostEqual(p["target"] - p["level"], 2 * (p["level"] - p["stop"]), places=1)

    def test_walk_outcomes(self):
        lv, st, tg = 103.0, 100.0, 109.0
        nope = bars_from([(101, 102.5, 100.5, 102)])
        self.assertEqual(S.walk(nope, 0, lv, st, tg)["result"], "nicht ausgelöst")
        hit = bars_from([(102, 104, 101.5, 103.5), (104, 109.5, 103.5, 109)])
        w = S.walk(hit, 0, lv, st, tg)
        self.assertEqual((w["result"], w["entry"], w["exit"], w["days"]), ("Ziel", 103.0, 109.0, 2))
        gap = bars_from([(103.5, 104, 102, 103), (97, 98, 96, 97)])
        w = S.walk(gap, 0, lv, st, tg)
        self.assertEqual((w["result"], w["exit"]), ("Stop (Lücke)", 97))          # the gap costs more than 1R
        opened_above = bars_from([(104, 105, 103.8, 104.5)] + [(104, 105, 103, 104)] * 9)
        w = S.walk(opened_above, 0, lv, st, tg)
        self.assertEqual((w["entry"], w["result"], w["days"]), (104, "Zeit (10 Tage)", 10))
        both = bars_from([(102, 110, 99, 105)])
        self.assertEqual(S.walk(both, 0, lv, st, tg)["result"], "Stop")         # the cautious reading
        running = bars_from([(102, 104, 101.5, 103.5), (104, 105, 103, 104)])
        w = S.walk(running, 0, lv, st, tg)
        self.assertEqual((w["entered"], w["done"], w["days"]), (True, False, 2))

    def test_backtest_takes_one_trade_at_a_time(self):
        rows = flat(25)
        for k in range(30):                       # a steady climb: a new 20-day high every day
            px = 100 + k
            rows.append((px, px + 1.5, px - 0.5, px + 1))
        bt = S.backtest(bars_from(rows), CFG)
        self.assertGreater(bt["n"], 0)
        days = [t["day"] for t in bt["trades"]]
        self.assertEqual(len(days), len(set(days)))
        self.assertLess(bt["n"], 30)              # overlapping signals are skipped
        self.assertIsNotNone(bt["avg_r_net"])
        self.assertLess(bt["avg_r_net"], bt["avg_r"])   # fees cost something


class Verdict(unittest.TestCase):
    def row(self, **kw):
        r = {"signal": True, "earnings": "nein", "hurdle_r": 0.3, "bt_n": 12, "bt_r_net": 0.2, "regel_ok": True}
        r.update(kw)
        return r

    def test_rules(self):
        self.assertEqual(S.verdict(self.row(), CFG), "Swing-Kandidat")
        for kw, want in [(dict(signal=False), "raus (kein 20-Tage-Hoch)"),
                         (dict(earnings="erwartet"), "raus (Zahlen in der Haltezeit)"),
                         (dict(earnings="gerade gemeldet"), "raus (Zahlen gerade gemeldet)"),
                         (dict(bt_n=5), "raus (zu wenig Fälle: 5)"),
                         (dict(bt_r_net=0.05), "raus (zu schwach nach Gebühren)"),
                         (dict(regel_ok=False), "raus (Regel zurzeit im Minus)"),
                         (dict(regel_ok=None), "raus (Regel-Check fehlt)")]:
            self.assertEqual(S.verdict(self.row(**kw), CFG), want, kw)


class Earnings(unittest.TestCase):
    def fetch(self, filings):
        tickers = {"0": {"ticker": "ABC", "cik_str": 123}}
        sub = {"filings": {"recent": {"form": [f for f, _, _ in filings], "filingDate": [d for _, d, _ in filings],
                                      "items": [i for _, _, i in filings]}}}

        def f(url, name):
            return json.dumps(tickers if "company_tickers" in url else sub)
        S._CIKS.clear()
        return f

    def test_windows(self):
        today = datetime.date(2026, 11, 9)
        cases = [([("8-K", "2026-11-08", "2.02"), ("10-Q", "2026-11-08", "")], "gerade gemeldet"),
                 ([("10-Q", "2026-08-20", "")], "erwartet"),           # next about 19.11.
                 ([("10-Q", "2026-10-29", ""), ("8-K", "2026-10-29", "2.02")], "nein"),
                 ([("6-K", "2026-11-02", "")], "unbekannt")]
        for filings, want in cases:
            self.assertEqual(S.earnings_window("ABC", today, self.fetch(filings))[0], want, filings)
        S._CIKS.clear()


if __name__ == "__main__":
    unittest.main()
