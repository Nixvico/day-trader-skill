import datetime
import json
import os
import tempfile
import unittest

from _load import load

L = load("trader-kandidaten", "labor")
K = L.K


def session(rng_h=101.0, rng_l=99.0, closes=None, low_after=None, high_after=None):
    """A 78-bar session: 3 range bars, then the given closes, then flat bars."""
    D = {}
    for i in range(3):
        D[i] = {"o": 100, "h": rng_h, "l": rng_l, "c": 100, "v": 1}
    closes = closes or []
    for j, c in enumerate(closes):
        D[3 + j] = {"o": c, "h": c, "l": c, "c": c, "v": 1}
    last = closes[-1] if closes else 100
    for i in range(3 + len(closes), 78):
        D[i] = {"o": last, "h": high_after or last, "l": low_after or last, "c": last, "v": 1}
    return D


class Orb(unittest.TestCase):
    CFG = dict(K.DEFAULTS)

    def test_entry_ten_minutes_later_and_target(self):
        # breakout bar 3 closes 101.5; two bars later 102.0 = entry; stop 100 (range middle); target 106
        D = session(closes=[101.5, 101.8, 102.0], high_after=106.5)
        t = L.orb_trade(D, self.CFG, delay=2)
        self.assertGreater(t["net"], 0)
        self.assertAlmostEqual(t["gross"] - t["net"], 2.0, places=2)

    def test_back_under_the_high_is_skipped(self):
        D = session(closes=[101.5, 101.0, 100.5])
        self.assertEqual(L.orb_trade(D, self.CFG, delay=2), {"skip": "unter Range-Hoch"})

    def test_stop(self):
        D = session(closes=[101.5, 101.8, 102.0], low_after=99.0)
        t = L.orb_trade(D, self.CFG, delay=2)
        self.assertLess(t["r"], -1.0)                    # the full risk plus the fees

    def test_summary_gate(self):
        good = [{"net": 1.0, "gross": 3.0, "r": 0.1}] * 400
        self.assertTrue(L.summarize(good, 300)["ok"])
        self.assertFalse(L.summarize(good[:100], 300)["ok"])
        bad = [{"net": -1.0, "gross": 1.0, "r": -0.1}] * 400
        self.assertEqual(L.summarize(bad, 300)["grund"], "die Regel verliert nach Gebühren")


def vwap_session(closes, vols):
    """Bars from 9:30: high/low 0.2 % around each close, slot numbers attached."""
    return [{"slot": i, "o": c, "h": c * 1.002, "l": c * 0.998, "c": c, "v": v} for i, (c, v) in enumerate(zip(closes, vols))]


class Vwap(unittest.TestCase):
    CFG = dict(K.DEFAULTS)

    def stock(self):
        # a steady climb, then a quiet dip that closes just above the VWAP, then a close above its high
        base = [100 + 0.5 * i for i in range(9)]
        vols = [1000] * 9
        dip = base[-1]
        for _ in range(50):                      # find a dip close 0.1 % above the VWAP including the dip itself
            vw, _, _ = L.session_lines(vwap_session(base + [dip], vols + [300]))
            dip = vw[-1] * 1.001
        closes = base + [dip, dip * 1.01] + [dip * 1.01 + 0.6 * i for i in range(1, 68)]
        return vwap_session(closes, vols + [300, 1500] + [1000] * 67)

    def test_pullback_long_with_the_market(self):
        mk = vwap_session([400 + 0.2 * i for i in range(78)], [1000] * 78)
        out = L.vwap_trades(self.stock(), mk, self.CFG)
        self.assertEqual(len(out), 1)
        self.assertGreater(out[0]["net"], 0)

    def test_no_trade_against_the_market(self):
        mk = vwap_session([400 - 0.2 * i for i in range(78)], [1000] * 78)
        self.assertEqual(L.vwap_trades(self.stock(), mk, self.CFG), [])


class Gate(unittest.TestCase):
    def test_gate_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "regel.json")
            with open(p, "w") as fh:
                json.dump({"orb": {"n": 500, "ok": False, "grund": "die Regel verliert nach Gebühren",
                                   "eur_pro_trade": -2.1}}, fh)
            ok, why = K.regel_gate(p, "orb")
            self.assertFalse(ok)
            self.assertIn("-2,10", why)
            self.assertEqual(K.regel_gate(p, "swing")[0], None)
        self.assertEqual(K.regel_gate(None, "orb")[0], None)


if __name__ == "__main__":
    unittest.main()
