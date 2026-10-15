"""Regression: Primary Genre field. TMDB fills it only when empty and never over a hand edit; smart-tag import sets it
unless hand-edited; backfill uses stored TMDB data only. Run: python -m unittest discover -s tests (temp DB, no network)."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {"id": 42, "title": "Some Film", "release_date": "1975-01-01", "genres": [{"id": 1, "name": "Drama"}, {"id": 2, "name": "Comedy"}]}
def fake_tmdb(path, key, **p):
    if path.startswith("search/"): return {"results": [dict(MOVIE)]}
    return MOVIE

class PrimaryGenre(unittest.TestCase):
    def setUp(self):
        self._orig = A.tmdb; A.tmdb = fake_tmdb
        c = db.connect(); db.set_setting(c, "tmdb_key", "test")
        self.fid = c.execute("INSERT INTO films(title,year,added_at,updated_at) VALUES('Some Film',1975,?,?)", (time.time(), time.time())).lastrowid
        db.get_tag_id(c, "DRAMA", "Genre"); db.add_tag(c, self.fid, "Heist", "Themes")
        c.commit(); c.close(); self.cl = A.app.test_client()
    def tearDown(self): A.tmdb = self._orig
    def film(self): return self.cl.get(f"/api/films/{self.fid}").get_json()

    def test_column_exists(self):
        self.assertIn("primary_genre", {r[1] for r in db.connect().execute("PRAGMA table_info(films)")})

    def test_tmdb_fills_empty_maps_case_and_keeps_tags(self):
        tags0 = {t["name"] for t in self.film()["tags"]}
        self.cl.post(f"/api/films/{self.fid}/tmdb", json={"tmdb_id": 42})
        f = self.film(); self.assertEqual(f["primary_genre"], "DRAMA")  # mapped to the existing Genre tag
        self.assertTrue(tags0 <= {t["name"] for t in f["tags"]})
        c = db.connect(); c.execute("UPDATE films SET primary_genre='Horror' WHERE id=?", (self.fid,)); c.commit()
        self.cl.post(f"/api/films/{self.fid}/tmdb", json={"tmdb_id": 42})
        self.assertEqual(self.film()["primary_genre"], "Horror")  # never replaces a non-empty value

    def test_user_edit_is_protected(self):
        self.cl.put(f"/api/films/{self.fid}", json={"primary_genre": ""})  # no change: not marked edited
        self.assertNotIn("primary_genre", json.loads(self.film()["edited_fields"] or "[]"))
        self.cl.put(f"/api/films/{self.fid}", json={"primary_genre": "Thriller"})
        self.assertIn("primary_genre", json.loads(self.film()["edited_fields"]))
        self.cl.post(f"/api/films/{self.fid}/tmdb", json={"tmdb_id": 42})
        c = db.connect(); db.import_ai_tags(c, [{"film_id": self.fid, "primary_genre": "Hardcore", "tags": []}])
        self.assertEqual(self.film()["primary_genre"], "Thriller")
        self.cl.put(f"/api/films/{self.fid}", json={"primary_genre": ""})  # cleared by hand: stays blank
        self.cl.post(f"/api/films/{self.fid}/tmdb", json={"tmdb_id": 42})
        self.assertIsNone(self.film()["primary_genre"])

    def test_smart_tag_import_sets_and_strips_pseudo_tag(self):
        c = db.connect(); c.execute("UPDATE films SET primary_genre='Drama' WHERE id=?", (self.fid,)); c.commit()
        r = db.import_ai_tags(c, [{"film_id": self.fid, "tags": ["Primary genre: hardcore", {"name": "Hardcore", "category": "Genre"}]}])
        self.assertEqual(r["primary_genres_set"], 1)
        f = self.film()
        self.assertEqual(f["primary_genre"].lower(), "hardcore")
        self.assertNotIn("Primary genre: hardcore", {t["name"] for t in f["pending"]})
        db.import_ai_tags(c, [{"film_id": self.fid, "Primary genre": "Pink film", "tags": []}])
        self.assertEqual(self.film()["primary_genre"], "Pink film")

    def test_backfill_from_stored_data_only(self):
        c = db.connect()
        a = c.execute("INSERT INTO films(title,tmdb_id,tmdb_genres) VALUES('A',1,?)", (json.dumps(["Horror", "Drama"]),)).lastrowid
        b = c.execute("INSERT INTO films(title,tmdb_id) VALUES('B',2)").lastrowid
        db.add_tag_if_absent(c, b, "Western", "Genre", source="tmdb"); db.add_tag_if_absent(c, b, "Action", "Genre", source="tmdb")
        n = c.execute("INSERT INTO films(title,tmdb_id) VALUES('C',3)").lastrowid; c.commit()
        A.tmdb = lambda *a, **k: self.fail("backfill must not call TMDB")
        r = self.cl.post("/api/primary-genre/backfill").get_json()
        g = lambda i: c.execute("SELECT primary_genre FROM films WHERE id=?", (i,)).fetchone()[0]
        self.assertEqual((g(a), g(b), g(n)), ("Horror", "Western", None))
        self.assertGreaterEqual(r["filled"], 2); self.assertGreaterEqual(r["no_cache_with_tmdb_id"], 1)

    def test_sort_and_filter(self):
        c = db.connect(); c.execute("UPDATE films SET primary_genre='Érotique' WHERE id=?", (self.fid,)); c.commit()
        j = self.cl.get("/api/films?pgenre=erotique").get_json(); self.assertIn(self.fid, [f["id"] for f in j["films"]])
        self.assertEqual(self.cl.get("/api/films?sort=primary_genre&order=desc").status_code, 200)
        self.assertNotIn(self.fid, [f["id"] for f in self.cl.get("/api/films?pgenre=(none)").get_json()["films"]])

if __name__ == "__main__": unittest.main()
