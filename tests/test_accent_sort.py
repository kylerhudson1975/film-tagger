"""Regression: accented titles sort with their base letter (É with E), case-insensitively, not after Z.
Run: python -m unittest discover -s tests   (temp DB; no network)."""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ:
    os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

TITLES = ["Zodiac", "Élan", "amélie", "Eraserhead", "Àbout Schmidt", "Ötzi", "Oldboy", "Brazil", "edge"]

class AccentSort(unittest.TestCase):
    def setUp(self):
        c = db.connect(); c.execute("DELETE FROM films")
        for t in TITLES:
            c.execute("INSERT INTO films(title,director,added_at,updated_at) VALUES(?,?,?,?)", (t, t, time.time(), time.time()))
        c.commit(); c.close(); self.cl = A.app.test_client()

    def titles(self, **q):
        r = self.cl.get("/api/films", query_string=dict(limit=100, **q)).get_json()
        rows = r["films"] if isinstance(r, dict) and "films" in r else r.get("items", r) if isinstance(r, dict) else r
        return [f["title"] for f in rows]

    def test_fold_key(self):
        self.assertEqual(db.fold_key("Élan"), "elan"); self.assertEqual(db.fold_key("Ötzi"), "otzi")

    def test_api_title_sort(self):
        got = self.titles(sort="title", order="asc")
        self.assertEqual(got, ["Àbout Schmidt", "amélie", "Brazil", "edge", "Élan", "Eraserhead", "Oldboy", "Ötzi", "Zodiac"])
        self.assertEqual(self.titles(sort="director", order="asc"), got)

    def test_sql_collation(self):
        c = db.connect()
        got = [r[0] for r in c.execute("SELECT title FROM films ORDER BY title COLLATE FOLD")]
        self.assertLess(got.index("Élan"), got.index("Zodiac")); c.close()

if __name__ == "__main__": unittest.main()
