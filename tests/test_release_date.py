"""Release Date replaces Year in the Edit window: partial dates accepted, year follows the date, edits protect both,
TMDB fills the date (not over hand edits), and the Release date sort uses the full date. Temp DB, mocked TMDB."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {"id": 138, "title": "Dracula", "release_date": "1931-02-12", "genres": [], "credits": {}}

class ReleaseDate(unittest.TestCase):
    def setUp(self):
        self._orig = A.tmdb; A.tmdb = lambda path, key, **p: dict(MOVIE)
        c = self.c = db.connect(); db.set_setting(c, "tmdb_key", "k")
        mk = lambda t, y, rd=None, ed=None: c.execute("INSERT INTO films(title,year,release_date,edited_fields,added_at) VALUES(?,?,?,?,?)", (t, y, rd, ed, time.time())).lastrowid
        self.drac = mk("Dracula", 1931); self.frank = mk("Frankenstein", 1931, "1931-11-21"); self.y = mk("Yearonly", 1931)
        self.hand = mk("Hand", 1930, None, json.dumps(["year"])); c.commit(); self.cl = A.app.test_client()
    def tearDown(self): A.tmdb = self._orig

    def test_normalize(self):
        n = A.norm_release_date
        self.assertEqual([n("1931"), n("1931-2"), n("1931/02/12"), n("")], ["1931", "1931-02", "1931-02-12", None])
        for bad in ("31", "1931-13", "1931-02-30", "abc"): self.assertRaises(ValueError, n, bad)

    def test_edit_sets_year_and_protects(self):
        j = self.cl.put(f"/api/films/{self.drac}", json={"release_date": "1932-02"}).get_json()
        self.assertEqual((j["release_date"], j["year"]), ("1932-02", 1932))
        self.assertTrue({"year", "release_date"} <= set(json.loads(j["edited_fields"])))
        self.assertEqual(self.cl.put(f"/api/films/{self.drac}", json={"release_date": "Feb"}).status_code, 400)
        j = self.cl.put(f"/api/films/{self.drac}", json={"release_date": ""}).get_json()
        self.assertEqual((j["release_date"], j["year"]), (None, 1932))  # clearing keeps the year

    def test_tmdb_fills_date_not_hand_year(self):
        A.apply_movie(self.c, self.drac, dict(MOVIE)); A.apply_movie(self.c, self.hand, dict(MOVIE)); self.c.commit()
        r = self.c.execute("SELECT release_date, year FROM films WHERE id=?", (self.drac,)).fetchone(); self.assertEqual(tuple(r), ("1931-02-12", 1931))
        r = self.c.execute("SELECT release_date, year FROM films WHERE id=?", (self.hand,)).fetchone(); self.assertEqual(tuple(r), (None, 1930))

    def test_tmdb_jan1_is_year_only(self):
        self.assertEqual([A.tmdb_date("1973-01-01"), A.tmdb_date("1931-02-12"), A.tmdb_date("")], ["1973", "1931-02-12", None])

    def test_sort(self):
        self.c.execute("UPDATE films SET release_date='1931-02-12' WHERE id=?", (self.drac,)); self.c.commit()
        rows = self.c.execute(f"SELECT f.id FROM films f WHERE f.id IN (?,?,?) ORDER BY {A.RELEASE_SORT}", (self.drac, self.frank, self.y)).fetchall()
        self.assertEqual([r[0] for r in rows], [self.y, self.drac, self.frank])

if __name__ == "__main__": unittest.main()
