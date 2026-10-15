"""Regression: 'Fill missing Primary Genres' job only writes tmdb_genres/primary_genre (no tags, no other metadata),
skips hand-edited / already-set / no-tmdb_id films, retries on 429. Temp DB, mocked TMDB, no network."""
import io, json, os, sys, tempfile, time, unittest, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class PGenreJob(unittest.TestCase):
    def setUp(self):
        self.calls = []; self._orig = A.tmdb; self.n429 = 1
        def fake(path, key, **p):
            self.calls.append(path)
            if self.n429: self.n429 -= 1; raise urllib.error.HTTPError(path, 429, "slow", {"Retry-After": "0.01"}, io.BytesIO())
            return {"id": 9, "title": "TMDB TITLE", "overview": "x", "genres": [{"name": "crime"}, {"name": "Drama"}]}
        A.tmdb = fake
        c = db.connect(); db.set_setting(c, "tmdb_key", "k"); db.get_tag_id(c, "Crime", "Genre")
        ins = lambda t, tid, pg=None, ed=None: c.execute("INSERT INTO films(title,tmdb_id,primary_genre,edited_fields,plot) VALUES(?,?,?,?,'mine')", (t, tid, pg, ed)).lastrowid
        self.a = ins("A", 9); self.b = ins("B", 9, "Horror"); self.e = ins("E", 9, None, json.dumps(["primary_genre"])); self.n = ins("N", None)
        db.add_tag(c, self.a, "Heist", "Themes"); c.commit(); self.c = c
    def tearDown(self): A.tmdb = self._orig

    def test_job(self):
        tags = lambda: sorted(self.c.execute("SELECT film_id, tag_id, status, source FROM film_tags").fetchall())
        t0 = tags(); ntags0 = self.c.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        ids = [(r[0], r[1]) for r in self.c.execute("SELECT id, tmdb_id FROM films WHERE " + A.PG_WHERE)]
        self.assertIn(self.a, [i for i, _ in ids]); self.assertFalse({self.b, self.e, self.n} & {i for i, _ in ids})
        A.JOB.update(running=True, kind="pgenre", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        A.run_pgenre_job("k", ids)
        r = self.c.execute("SELECT title, plot, primary_genre, tmdb_genres FROM films WHERE id=?", (self.a,)).fetchone()
        self.assertEqual((r[0], r[1], r[2]), ("A", "mine", "Crime")); self.assertEqual(json.loads(r[3]), ["crime", "Drama"])
        self.assertEqual(A.JOB["errors"], 0); self.assertGreaterEqual(A.JOB["matched"], 1)
        self.assertEqual(tags(), t0); self.assertEqual(self.c.execute("SELECT COUNT(*) FROM tags").fetchone()[0], ntags0)
        g = lambda i: self.c.execute("SELECT primary_genre FROM films WHERE id=?", (i,)).fetchone()[0]
        self.assertEqual((g(self.b), g(self.e), g(self.n)), ("Horror", None, None))

    def test_status_endpoint(self):
        j = A.app.test_client().get("/api/pgenre/status").get_json()
        self.assertIn("remaining", j); self.assertIn("job", j)

if __name__ == "__main__": unittest.main()
