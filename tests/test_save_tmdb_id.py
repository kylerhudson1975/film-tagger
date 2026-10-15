"""Regression: pasting a TMDB id in the Edit window and clicking Save loads TMDB details immediately (one Save),
keeping hand-edited fields and this save's own changes, and never removing/changing tags. Temp DB, mocked TMDB."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {"id": 777, "title": "TMDB Title", "release_date": "1981-02-02", "runtime": 88, "overview": "TMDB plot", "poster_path": "/p.jpg",
         "genres": [{"name": "Horror"}], "credits": {"cast": [{"name": "Actor X"}], "crew": [{"job": "Director", "name": "TMDB Dir"}]}}

class SaveTmdbId(unittest.TestCase):
    def setUp(self):
        self._orig = A.tmdb; self.calls = []
        A.tmdb = lambda path, key, **p: (self.calls.append(path), dict(MOVIE))[1]
        c = db.connect(); db.set_setting(c, "tmdb_key", "k")
        self.fid = c.execute("INSERT INTO films(title,director,edited_fields,added_at) VALUES('my title','Mine',?,?)", (json.dumps(["director"]), time.time())).lastrowid
        db.add_tag(c, self.fid, "Keepme", "Themes"); db.add_tag(c, self.fid, "Pend", "Mood", source="ai"); c.commit(); self.cl = A.app.test_client()
    def tearDown(self): A.tmdb = self._orig

    def test_single_save_loads_metadata(self):
        r = self.cl.put(f"/api/films/{self.fid}", json={"title": "my title", "notes": "typed now", "plot": "", "tmdb_id": "https://www.themoviedb.org/movie/777-x"})
        j = r.get_json(); self.assertTrue(j["tmdb_loaded"])
        self.assertEqual((j["tmdb_id"], j["year"], j["runtime"], j["plot"]), (777, 1981, 88, "TMDB plot"))
        self.assertEqual(j["poster"], "https://image.tmdb.org/t/p/w342/p.jpg")
        self.assertEqual(j["director"], "Mine"); self.assertEqual(j["notes"], "typed now")      # hand edit + this save's change kept
        self.assertEqual({t["name"] for t in j["tags"]}, {"Keepme", "Horror"}); self.assertEqual([t["name"] for t in j["pending"]], ["Pend"])
        n = len(self.calls); self.cl.put(f"/api/films/{self.fid}", json={"tmdb_id": "777", "notes": "again"})
        self.assertEqual(len(self.calls), n, "unchanged tmdb_id must not refetch")

    def test_create_with_tmdb_id(self):
        j = self.cl.post("/api/films", json={"title": "New", "tmdb_id": "777"}).get_json()
        self.assertEqual((j["title"], j["year"], j["plot"]), ("TMDB Title", 1981, "TMDB plot"))

if __name__ == "__main__": unittest.main()
