"""Regression: tag filters and text search ignore case, accents, spaces, hyphens, underscores and a plural 's',
but never merge names that differ otherwise (e.g. 'Cum' vs 'Cum (Kink)'). Run: python -m unittest discover -s tests"""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ:
    os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

FILMS = {"A": ["Cum play"], "B": ["Cumplay"], "C": ["Cumshot"], "D": ["Cumshots"], "E": ["Cum"], "F": ["Cum (Kink)"], "G": ["Ass"], "H": ["Asses"]}

class TagNorm(unittest.TestCase):
    def setUp(self):
        c = db.connect(); c.execute("DELETE FROM film_tags"); c.execute("DELETE FROM films"); c.execute("DELETE FROM tags")
        for t, tags in FILMS.items():
            fid = c.execute("INSERT INTO films(title,added_at,updated_at) VALUES(?,?,?)", (t, time.time(), time.time())).lastrowid
            for n in tags: db.add_tag(c, fid, n, "Content")
        c.commit(); c.close(); self.cl = A.app.test_client()

    def got(self, **q):
        return sorted(f["title"] for f in self.cl.get("/api/films", query_string=dict(limit=100, **q)).get_json()["films"])

    def test_norm(self):
        self.assertEqual(db.tag_norm("Cum-Play"), "cumplay"); self.assertEqual(db.tag_norm("Cumshots"), "cumshot")
        self.assertEqual(db.tag_norm("Ass"), "ass"); self.assertEqual(db.tag_norm("Sms"), "sms"); self.assertEqual(db.tag_norm("Éclairs"), "eclair")
        self.assertNotEqual(db.tag_norm("Cum"), db.tag_norm("Cum (Kink)"))

    def test_filters(self):
        for k in ("all", "any"):
            self.assertEqual(self.got(**{k: "cumplay"}), ["A", "B"])
            self.assertEqual(self.got(**{k: "Cum play"}), ["A", "B"])
            self.assertEqual(self.got(**{k: "cumshot"}), ["C", "D"])
            self.assertEqual(self.got(**{k: "Cumshots"}), ["C", "D"])
            self.assertEqual(self.got(**{k: "Cum"}), ["E"])          # chip click: no over-match of 'Cum (Kink)'
            self.assertEqual(self.got(**{k: "Cum (Kink)"}), ["F"])
            self.assertEqual(self.got(**{k: "Ass"}), ["G"])           # 'Ass' isn't stripped to 'as'
        self.assertNotIn("A", self.got(none="cumplay")); self.assertNotIn("B", self.got(none="cum-play"))

    def test_text_search(self):
        self.assertEqual(self.got(q="cumplay"), ["A", "B"])
        self.assertEqual(self.got(q="cumshots"), ["C", "D"])
        self.assertEqual(self.got(q="cumshot", **{"in": "tags"}), ["C", "D"])
        self.assertEqual(self.got(q="cumplay", **{"in": "tags"}), ["A", "B"])

if __name__ == "__main__": unittest.main()
