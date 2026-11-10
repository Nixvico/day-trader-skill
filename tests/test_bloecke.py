import datetime
import unittest

from _load import load

B = load("trader-kandidaten", "bloecke")
K = B.K
CFG = dict(K.DEFAULTS)


def series(start_px, step, n=260, start=datetime.date(2025, 1, 1)):
    """n weekday bars rising by `step` per day (a factor)."""
    out, d, px = [], start, start_px
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"d": d, "o": px, "h": px, "l": px, "c": px})
            px *= step
        d += datetime.timedelta(days=1)
    return out


def data():
    return {"SPY": series(500, 1.0005), "FAST": series(10, 1.004), "MID": series(10, 1.002),
            "SLOW": series(10, 1.001), "DOWN": series(10, 0.999)}


class Ranking(unittest.TestCase):
    def test_strongest_first_and_trend_filter(self):
        d = data()
        day = d["SPY"][-1]["d"]
        r = [s for _, s, _ in B.ranking(d, day)]
        self.assertEqual(r, ["FAST", "MID", "SLOW"])          # DOWN is under its 200-day average, SPY excluded


class Tick(unittest.TestCase):
    def test_start_rotate_and_fees(self):
        d = data()
        days = [b["d"] for b in d["SPY"]]
        day0 = days[230]
        state, news = B.tick({}, d, day0, CFG)
        self.assertEqual([p["ticker"] for p in state["A"]["positionen"]], ["FAST"])
        self.assertEqual([p["ticker"] for p in state["B"]["positionen"]], ["FAST", "MID", "SLOW"])
        self.assertEqual([p["ticker"] for p in state["C"]["positionen"]], ["SPY"])
        self.assertEqual(len(news), 3)
        self.assertAlmostEqual(state["B"]["gebuehren_eur"], 3.0)
        self.assertAlmostEqual(state["A"]["wert_eur"], 499.0, places=1)    # 1 EUR to buy
        # 24 sessions later: still the same block
        state, news = B.tick(state, d, days[254], CFG)
        self.assertEqual(news, [])
        self.assertEqual(state["A"]["tage_im_block"], 24)
        # 25 sessions: the block closes and the next one starts
        state, news = B.tick(state, d, days[255], CFG)
        self.assertEqual(len(news), 2)
        a = state["A"]
        self.assertEqual(a["block_nr"], 2)
        self.assertEqual(len(a["bloecke"]), 1)
        self.assertGreater(a["bloecke"][0]["ergebnis_eur"], 0)
        self.assertAlmostEqual(a["gebuehren_eur"], 3.0)                    # buy, sell, buy
        self.assertEqual(state["C"]["block_nr"], 1)                        # the market is never sold

    def test_planned_end_counts_sessions(self):
        d = data()
        days = [b["d"] for b in d["SPY"]]
        self.assertEqual(B.planned_end(d, days[200]), days[225])


if __name__ == "__main__":
    unittest.main()
