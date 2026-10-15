"""Regression: films with a file under 'Classic Porn' never get a TMDB genre (Comedy, Drama...) as primary_genre.
TMDB save-with-id, the Fill-missing-Primary-Genres job, backfill and smart-tag import only ever give Hardcore/Softcore.
Temp DB, mocked TMDB, no network."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {"id": 555, "title": "Classic", "release_date": "1978-01-01", "overview": "plot",
         "genres": [{"name": "Comedy"}, {"name": "Drama"}], "credits": {"cast": [], "crew": []}}

class ClassicPornPrimaryGenre(unittest.TestCase):
    def setUp(self):
        self._orig = A.tmdb; A.tmdb = lambda path, key, **p: dict(MOVIE)
        c = db.connect(); db.set_setting(c, "tmdb_key", "k"); db.get_tag_id(c, "Softcore", "Genre"); db.get_tag_id(c, "Hardcore", "Genre")
        def film(title, path, tmdb_id=None):
            fid = c.execute("INSERT INTO films(title,tmdb_id,added_at) VALUES(?,?,?)", (title, tmdb_id, time.time())).lastrowid
            c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path.replace(".mkv", f"{fid}.mkv"))); return fid
        self.hard = film("Hard " + str(time.time()), r"D:\Classic Porn\Hard (1978)\hard.mkv")
        self.soft = film("Soft " + str(time.time()), r"D:\Classic Porn\Soft (1975)\soft.mkv")
        db.add_tag(c, self.soft, "Pink film", "Genre")
        self.jobhard = film("JobHard " + str(time.time()), "D:/Classic Porn/x/jh.mkv", 555)
        self.jobsoft = film("JobSoft " + str(time.time()), r"D:\Classic Porn\y\js.mkv", 555); db.add_tag(c, self.jobsoft, "Not hardcore porn", "Other")
        self.normal = film("Normal " + str(time.time()), r"D:\Films\n.mkv", 555)
        c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): A.tmdb = self._orig; self.c.rollback(); self.c.close()
    pg = lambda self, i: self.c.execute("SELECT primary_genre FROM films WHERE id=?", (i,)).fetchone()[0]

    def test_save_with_tmdb_id(self):
        self.cl.put(f"/api/films/{self.hard}", json={"tmdb_id": "555"})
        self.cl.put(f"/api/films/{self.soft}", json={"tmdb_id": "555"})
        self.assertEqual((self.pg(self.hard), self.pg(self.soft)), ("Hardcore", "Softcore"))

    def test_fill_job(self):
        ids = [i for i, _ in self.c.execute("SELECT id, tmdb_id FROM films WHERE " + A.PG_WHERE)]
        self.assertNotIn(self.jobhard, ids); self.assertNotIn(self.jobsoft, ids); self.assertIn(self.normal, ids)
        A.JOB["running"] = False
        self.assertEqual(self.cl.post("/api/pgenre/start").status_code, 200)
        for _ in range(100):
            if not A.JOB["running"]: break
            time.sleep(0.05)
        self.assertEqual((self.pg(self.jobhard), self.pg(self.jobsoft), self.pg(self.normal)), ("Hardcore", "Softcore", "Comedy"))

    def test_any_setter_and_backfill(self):
        self.c.execute("UPDATE films SET tmdb_genres=? WHERE id=?", (json.dumps(["Romance"]), self.hard))
        db.backfill_primary_genre(self.c); self.assertEqual(self.pg(self.hard), "Hardcore")
        db.set_primary_genre(self.c, self.soft, "Drama", only_if_empty=False); self.assertEqual(self.pg(self.soft), "Softcore")
        cat = self.c.execute("SELECT category FROM tags WHERE name='Softcore'").fetchone()[0]; self.assertEqual(cat, "Genre")

if __name__ == "__main__": unittest.main()
