"""Regression: Classic Porn films never keep NC-17 as Rating (MPAA); TMDB fill, MPAA job and saves map it to X.
Movies/Scenes outside Classic Porn keep NC-17. Temp DB, mocked TMDB, no network."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {"id": 777, "title": "Classic", "release_date": "1978-01-01", "overview": "plot", "poster_path": None,
         "genres": [{"name": "Comedy"}], "credits": {"cast": [], "crew": []},
         "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [{"type": 3, "certification": "NC-17"}]}]}}

class ClassicPornMpaa(unittest.TestCase):
    def setUp(self):
        self._orig = A.tmdb
        A.tmdb = lambda path, key, **p: (MOVIE["release_dates"] if path.endswith("/release_dates") else dict(MOVIE))
        c = db.connect(); db.set_setting(c, "tmdb_key", "k")
        def film(title, path, tmdb_id=None, mpaa=None):
            fid = c.execute("INSERT INTO films(title,tmdb_id,mpaa,added_at) VALUES(?,?,?,?)",
                            (title, tmdb_id, mpaa, time.time())).lastrowid
            c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path.replace(".mkv", f"{fid}.mkv")))
            return fid
        self.cp = film("CP " + str(time.time()), r"D:\Classic Porn\A (1978)\a.mkv", 777)
        self.cp_empty = film("CPEmpty " + str(time.time()), r"D:\Classic Porn\B (1979)\b.mkv", 777, mpaa=None)
        self.movie = film("Movie " + str(time.time()), r"D:\Movies\m.mkv", 777)
        self.movie_empty = film("MovieEmpty " + str(time.time()), r"D:\Movies\n.mkv", 777, mpaa=None)
        c.commit(); self.c = c; self.cl = A.app.test_client()

    def tearDown(self):
        A.tmdb = self._orig; self.c.rollback(); self.c.close()

    def mpaa(self, i):
        return self.c.execute("SELECT mpaa FROM films WHERE id=?", (i,)).fetchone()[0]

    def test_helpers(self):
        for v in ("NC-17", "nc-17", "NC 17", "nc17", " NC-17 ", "NC_17"):
            self.assertTrue(db.is_nc17(v), v)
            self.assertEqual(db.classic_porn_mpaa(self.c, self.cp, v), "X")
            self.assertEqual(db.classic_porn_mpaa(self.c, self.movie, v), v)
        self.assertEqual(db.classic_porn_mpaa(self.c, self.cp, "R"), "R")
        self.assertEqual(db.classic_porn_mpaa(self.c, self.cp, None), None)

    def test_apply_movie_maps_nc17(self):
        A.apply_movie(self.c, self.cp, MOVIE); self.c.commit()
        self.assertEqual(self.mpaa(self.cp), "X")
        A.apply_movie(self.c, self.movie, MOVIE); self.c.commit()
        self.assertEqual(self.mpaa(self.movie), "NC-17")

    def test_mpaa_job(self):
        A.JOB.update(running=True, kind="mpaa", done=0, total=2, matched=0, skipped=0, errors=0, error=None, cancel=False)
        A.run_mpaa_job("k", [(self.cp_empty, 777), (self.movie_empty, 777)])
        self.assertEqual(self.mpaa(self.cp_empty), "X")
        self.assertEqual(self.mpaa(self.movie_empty), "NC-17")

    def test_put_and_bulk(self):
        self.cl.put(f"/api/films/{self.cp}", json={"mpaa": "NC-17"})
        self.assertEqual(self.mpaa(self.cp), "X")
        self.cl.put(f"/api/films/{self.movie}", json={"mpaa": "NC-17"})
        self.assertEqual(self.mpaa(self.movie), "NC-17")
        r = self.cl.post("/api/bulk/mpaa", json={"film_ids": [self.cp, self.movie], "mpaa": "nc 17"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((self.mpaa(self.cp), self.mpaa(self.movie)), ("X", "nc 17"))

if __name__ == "__main__":
    unittest.main()
