"""ThePornDB multi-strategy matching: cleaning, studio split, scoring, strategy order, retries. No network."""
import os, sys, unittest, urllib.error, io
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(HERE))
import tpdb

SC = {"id": "u1", "title": "Perfect Moment - S7:E8", "date": "2022-12-04", "site": {"name": "Girls Only Porn"},
      "performers": [{"name": "Alexis Crystal"}, {"name": "Agatha Vega"}]}
OTHER = {"id": "u2", "title": "Something Else", "date": "2021-01-01", "site": {"name": "Girls Only Porn"}, "performers": [{"name": "Jane Doe"}]}

class Match(unittest.TestCase):
    def test_clean_and_split(self):
        self.assertEqual(tpdb.clean_stem("Lesbea Gigi Max And Sarah Heizel 19 08 2026 1080p PornHoarder.tv"), "Lesbea Gigi Max And Sarah Heizel 19 08 2026")
        self.assertEqual(tpdb.clean_stem("MissaX-River Lynn Golden 4KPorn"), "MissaX - River Lynn Golden")
        self.assertEqual(tpdb.clean_stem("Mommysgirl Aderes Quin Green eyed Stepmom Watch on Po"), "Mommysgirl Aderes Quin Green eyed Stepmom")
        self.assertEqual(tpdb.split_studio("Mommysgirl Serene Siren", "Mommy's Girl"), ("Mommy's Girl", "Serene Siren"))
        self.assertEqual(tpdb.split_studio("SisLovesMe Claire Roos", None), ("Sis Loves Me", "Claire Roos"))
        self.assertEqual(tpdb.split_studio("Elise London Lucy Foxxx", "_No Studio"), (None, "Elise London Lucy Foxxx"))
        self.assertEqual(tpdb._date("x 19 08 2026")[0], "2026-08-19")

    def test_score(self):
        s, why = tpdb.score(SC, "Girls Only Porn", None, "GirlsOnlyPorn Alexis Crystal Agatha Vega Perfect Moment")
        self.assertGreaterEqual(s, tpdb.CONFIDENT); self.assertIn("studio", why)
        self.assertEqual(tpdb.score(SC, "Girls Only Porn", "2022-12-05", "x")[0], -1)  # date clash
        self.assertLess(tpdb.score(OTHER, "Girls Only Porn", None, "Alexis Crystal Agatha Vega Perfect Moment")[0], tpdb.CONFIDENT)
        # truncated filename title still counts
        t = {"title": "Oh Fuck! You're Not My Wife! Jealous Julia James Wants A Turn", "site": {"name": "Dad Crush"}, "performers": [{"name": "Julia James"}]}
        self.assertGreaterEqual(tpdb.score(t, "Dad Crush", None, "DadCrush Oh Fuck Youre Not My Wife Jealous Julia James Wants a T")[0], tpdb.CONFIDENT)

    def test_no_false_positives(self):
        ist = {"title": "Claire Roos", "site": {"name": "iStripper"}, "performers": [{"name": "Claire Roos"}]}
        self.assertFalse(tpdb.is_confident(*tpdb.score(ist, "Sis Loves Me", None, "Sis Loves Me Claire Roos", ["Sis Loves Me"])))
        sd = {"title": "Your Choice", "date": "2024-08-09", "site": {"name": "Sis Loves Me"}, "performers": [{"name": "Trinity Olsen"}]}
        self.assertFalse(tpdb.is_confident(*tpdb.score(sd, "Sis Loves Me", "2024-08-09", "Sis Loves Me Sweet Cousin In Town Emma Rosie", [])))
        net = {"title": "Tricky Poses", "site": {"name": "MILF Body", "network": {"name": "MYLF"}}, "performers": [{"name": "Penny Barber"}]}
        self.assertTrue(tpdb.is_confident(*tpdb.score(net, "MYLF", None, "MYLF Tricky Poses MilfBody", [])))

    def test_no_studio_but_all_performers_and_title(self):
        sc = {"title": "Downward Temptation", "site": {"name": "Girlsway"}, "performers": [{"name": "Blake Blossom"}, {"name": "Romi Rain"}]}
        self.assertTrue(tpdb.is_confident(*tpdb.score(sc, None, None, "Romi Rain Blake Blossom Downward Temptation", [])))
        one = {"title": "Step Sis Wants To Get Over Boyfriend", "site": {"name": "Step Fam POV"}, "performers": [{"name": "Violet Moon"}]}
        self.assertFalse(tpdb.is_confident(*tpdb.score(one, None, None, "Violet Moon Step Sis Wants To Get Over Boyfriend", [])))

    def test_identify_strategies(self):
        calls = []
        def fetch(path, **q):
            calls.append((path, q))
            if path == "/sites": return {"data": [{"name": "Girls Only Porn"}]}
            if "parse" in q: return {"data": []}
            return {"data": [OTHER, SC]}
        r = tpdb.identify("t", r"D:\Porn Scenes\Girls Only Porn\GirlsOnlyPorn Alexis Crystal Agatha Vega Perfect Moment.mp4", fetch=fetch)
        self.assertTrue(r["confident"]); self.assertEqual(r["scene"]["id"], "u1")
        self.assertEqual([c[0] for c in calls][:2], ["/sites", "/scenes"])
        self.assertIn("parse", calls[1][1])
        r = tpdb.identify("t", r"D:\x\_No Studio\swollen labia 0001.mp4", fetch=lambda p, **q: {"data": [OTHER]})
        self.assertFalse(r and r["confident"])

    def test_retry(self):
        n = {"i": 0}; orig = tpdb.call; sl = tpdb.time.sleep; tpdb.time.sleep = lambda s: None
        def flaky(path, token, **p):
            n["i"] += 1
            if n["i"] < 3: raise urllib.error.HTTPError(path, 429, "slow", {}, io.BytesIO())
            return {"data": []}
        tpdb.call = flaky
        try: self.assertEqual(tpdb.call_retry("/scenes", "t", q="x"), {"data": []}); self.assertEqual(n["i"], 3)
        finally: tpdb.call = orig; tpdb.time.sleep = sl

if __name__ == "__main__": unittest.main()
