"""Regression: Fetch from TMDB / refresh / backfills must never remove or change tags (approved or pending) or
user-owned fields. Run: python -m unittest discover -s tests   (uses a temp DB and a mocked TMDB; no network)."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(TMP, "test.db"); sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {"id": 334728, "title": "Naughty Nanny Diaries", "original_title": "Naughty Nanny Diaries", "release_date": "2010-05-01",
         "runtime": 95, "overview": "TMDB plot", "poster_path": "/nanny.jpg", "genres": [{"id": 35, "name": "Comedy"}, {"id": 18, "name": "Drama"}],
         "credits": {"cast": [{"name": "TMDB Actor"}], "crew": [{"job": "Director", "name": "TMDB Director"}, {"job": "Writer", "name": "TMDB Writer"}]},
         "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": [{"type": 3, "certification": "NC-17"}]}]},
         "alternative_titles": {"titles": [{"title": "Nanny Alt"}]},
         "production_countries": [{"name": "United States of America"}], "production_companies": [{"name": "TMDB Studio"}]}
def fake_tmdb(path, key, **p):
    if path.startswith("search/"): return {"results": [dict(MOVIE)]}
    if path.endswith("/release_dates"): return MOVIE["release_dates"]
    return MOVIE
A.tmdb = fake_tmdb

class TmdbKeepsTags(unittest.TestCase):
    def setUp(self):
        c = db.connect(); db.set_setting(c, "tmdb_key", "test")
        self.fid = c.execute("INSERT INTO films(title,year,rating,watched,notes,director,custom_poster,mpaa,storage,user_akas,edited_fields,added_at,updated_at)"
            " VALUES('Naughty Nanny Diaries',2010,8,1,'my notes','My Director','custom.jpg','X','Shelf 3','Nanny Diaries XXX',?,?,?)",
            (json.dumps(["director", "mpaa"]), time.time(), time.time())).lastrowid
        db.add_tag(c, self.fid, "Babysitter", "Themes"); db.add_tag(c, self.fid, "Suburbia", "Setting")
        db.add_tag(c, self.fid, "Comedy", None, source="ai")   # pending tag that matches a TMDB genre name
        db.add_tag(c, self.fid, "Voyeurism", "Themes", source="ai")
        c.commit(); c.close(); self.cl = A.app.test_client()

    def state(self):
        f = self.cl.get(f"/api/films/{self.fid}").get_json()
        return f, {(t["name"], t["category"]) for t in f["tags"]}, {(t["name"], t["category"]) for t in f["pending"]}

    def assert_user_data_intact(self, f):
        for k, v in dict(rating=8, watched=1, notes="my notes", custom_poster="custom.jpg", director="My Director",
                         mpaa="X", storage="Shelf 3", user_akas="Nanny Diaries XXX").items():
            self.assertEqual(f[k], v, k)

    def test_fetch_then_save_keeps_tags(self):
        _, tags0, pend0 = self.state()
        r = self.cl.post(f"/api/films/{self.fid}/tmdb", json={"tmdb_id": 334728}); self.assertEqual(r.status_code, 200)
        f, tags1, pend1 = self.state()
        self.assertTrue(tags0 <= tags1, "approved tags lost by fetch"); self.assertEqual(pend1, pend0, "pending tags changed by fetch")
        self.assertEqual(tags1 - tags0, {("Drama", "Genre")})       # only genuinely new genre tags are added
        self.assertEqual(f["poster"], "https://image.tmdb.org/t/p/w342/nanny.jpg"); self.assert_user_data_intact(f)
        form = {k: f[k] for k in ("title", "original_title", "user_akas", "year", "runtime", "director", "rating", "mpaa", "watched", "cast_",
                                  "writers", "producers", "cinematographer", "composer", "country", "studio", "resolution", "plot", "notes",
                                  "storage", "tmdb_id", "poster")}
        form = {k: ("" if v is None else v) for k, v in form.items()}; form["paths"] = []
        self.assertEqual(self.cl.put(f"/api/films/{self.fid}", json=form).status_code, 200)              # Save right after the fetch
        self.assertEqual(self.cl.put(f"/api/films/{self.fid}", json=dict(form, tags=[])).status_code, 200)  # a stray empty tags list
        f2, tags2, pend2 = self.state()
        self.assertEqual(tags2, tags1); self.assertEqual(pend2, pend0); self.assert_user_data_intact(f2)

    def test_backfills_keep_tags(self):
        c = db.connect(); c.execute("UPDATE films SET tmdb_id=334728 WHERE id=?", (self.fid,)); c.commit()
        _, tags0, pend0 = self.state()
        A.JOB["cancel"] = False
        A.run_credits_job("k", [(self.fid, 334728)]); A.run_titles_job("k", [(self.fid, 334728)])
        A.run_country_job("k", [(self.fid, 334728)]); A.run_studio_job("k", [(self.fid, 334728)]); A.run_mpaa_job("k", [(self.fid, 334728)])
        A.run_poster_job("k", [self.fid])
        f, tags1, pend1 = self.state()
        self.assertTrue(tags0 <= tags1); self.assertEqual(pend1, pend0); self.assert_user_data_intact(f)

if __name__ == "__main__": unittest.main()
