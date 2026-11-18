"""Not a scene: move a mis-filed full film from Porn Scenes into Classic Porn as the SAME record (tags, rating kept),
named '<Classic Porn>/Title (Year)/Title (Year).ext'; a later scan adds no duplicate; collisions/missing files change
nothing. TMDB is mocked. Run: python tests/test_to_classic.py"""
import os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class ToClassic(unittest.TestCase):
    def setUp(self):
        t = tempfile.mkdtemp(); self.sr = os.path.join(t, "Porn Scenes"); self.cr = os.path.join(t, "Classic Porn")
        os.makedirs(os.path.join(self.sr, "Some Studio")); os.makedirs(self.cr)
        c = db.connect(); db.set_setting(c, "scene_roots", [self.sr]); db.set_setting(c, "roots", [self.cr]); db.set_setting(c, "tmdb_key", "")
        c.execute("DELETE FROM films"); c.execute("DELETE FROM files"); c.commit(); self.c = c; self.cl = A.app.test_client()
        self.old = os.path.join(self.sr, "Some Studio", "Behind.The.Green.Door.1972.mp4"); 
        with open(self.old, "wb") as fh: fh.write(b"v" * 1000)
        with open(self.old[:-4] + ".en.srt", "w") as fh: fh.write("1")
        self.fid = c.execute("INSERT INTO films(title,kind,tpdb_id,release_date,year,rating,watched,studio,cast_,primary_genre,added_at) "
                             "VALUES('Behind the Green Door','scene','tp1','1972-12-01',1972,8,1,'Some Studio','Marilyn Chambers','Hardcore',?)", (time.time(),)).lastrowid
        c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,1000)", (self.fid, self.old)); db.add_tag(c, self.fid, "Vintage", "Themes"); c.commit()
    def tearDown(self): self.c.close()
    def row(self): return self.c.execute("SELECT * FROM films WHERE id=?", (self.fid,)).fetchone()

    def test_prefill_and_dry_run_touch_nothing(self):
        p = self.cl.get(f"/api/films/{self.fid}/to-classic").get_json()
        self.assertEqual((p["title"], p["year"]), ("Behind the Green Door", 1972))
        d = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door", year=1972, dry_run=True)).get_json()
        self.assertTrue(d["new"].endswith(os.path.join("Classic Porn", "Behind the Green Door (1972)", "Behind the Green Door (1972).mp4")))
        self.assertTrue(os.path.exists(self.old)); self.assertEqual(self.row()["kind"], "scene")

    def test_move_keeps_record_and_scan_adds_no_duplicate(self):
        r = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door", year="1972"))
        self.assertEqual(r.status_code, 200, r.get_json())
        new = os.path.join(self.cr, "Behind the Green Door (1972)", "Behind the Green Door (1972).mp4")
        self.assertTrue(os.path.isfile(new)); self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.isfile(new[:-4] + ".en.srt")); self.assertFalse(os.path.isdir(os.path.dirname(self.old)))  # empty studio folder dropped
        f = self.row()
        self.assertEqual((f["kind"], f["tpdb_id"], f["rating"], f["watched"], f["cast_"], f["primary_genre"]), (None, None, 8, 1, "Marilyn Chambers", "Hardcore"))
        self.assertEqual([x[0] for x in self.c.execute("SELECT t.name FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=?", (self.fid,))], ["Vintage"])
        self.assertTrue(db.is_classic_porn(self.c, self.fid)); self.assertNotIn(self.fid, A.soft_match_ids(self.c))
        n = self.c.execute("SELECT COUNT(*) FROM films").fetchone()[0]
        db.scan(self.c, [self.cr]); self.c.commit()
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM films").fetchone()[0], n)
        self.assertEqual(self.c.execute("SELECT film_id FROM files WHERE path=?", (new,)).fetchone()[0], self.fid)
        self.assertIsNone(self.row()["kind"])

    def test_tmdb_match_keeps_porn_primary_genre(self):
        db.set_setting(self.c, "tmdb_key", "k"); self.c.commit()
        m = {"id": 5, "title": "Behind the Green Door", "release_date": "1972-12-17", "genres": [{"name": "Comedy"}], "credits": {"cast": [], "crew": []}}
        with mock.patch.object(A, "search_tmdb_movie_then_tv", return_value=({"id": 5}, "movie")), mock.patch.object(A, "tmdb_get", return_value=m):
            j = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door", year=1972)).get_json()
        self.assertEqual(j["match_note"], 'Searched TMDB for "Behind the Green Door" (1972): matched to Behind the Green Door (1972)'); f = self.row()
        self.assertEqual(f["tmdb_id"], 5); self.assertIn(f["primary_genre"], ("Hardcore", "Softcore"))

    def test_collision_and_missing_change_nothing(self):
        os.makedirs(os.path.join(self.cr, "Behind the Green Door (1972)"))
        r = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door", year=1972))
        self.assertEqual(r.status_code, 400); self.assertIn("collision", r.get_json()["error"])
        self.assertTrue(os.path.exists(self.old)); self.assertEqual(self.row()["kind"], "scene")
        os.remove(self.old)
        r = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Other", year=1980))
        self.assertEqual(r.status_code, 400); self.assertIn("missing", r.get_json()["error"]); self.assertEqual(self.row()["tpdb_id"], "tp1")

    def test_other_drive_copies_checks_size_then_deletes(self):
        with mock.patch.object(A.db, "same_drive", return_value=False):
            r = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door", year=1972))
        self.assertEqual(r.status_code, 200); self.assertFalse(os.path.exists(self.old))
        self.assertEqual(os.path.getsize(os.path.join(self.cr, "Behind the Green Door (1972)", "Behind the Green Door (1972).mp4")), 1000)

    def test_year_in_title_not_doubled_and_no_match_is_marked(self):
        # Kyle's real case: the scene title already ended in '(2021)'
        self.c.execute("UPDATE films SET title='Behind the Green Door (1972)' WHERE id=?", (self.fid,)); db.set_setting(self.c, "tmdb_key", "k"); self.c.commit()
        p = self.cl.get(f"/api/films/{self.fid}/to-classic").get_json(); self.assertEqual((p["title"], p["year"]), ("Behind the Green Door", 1972))
        with mock.patch.object(A, "search_tmdb_movie_then_tv", return_value=(None, None)) as s:
            j = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door (1972)", year=1972)).get_json()
        s.assert_called_once(); self.assertEqual(s.call_args[0][1:], ("Behind the Green Door", 1972))
        self.assertIn("no confident TMDB match; marked Needs TMDB match", j["match_note"])
        self.assertTrue(os.path.isfile(os.path.join(self.cr, "Behind the Green Door (1972)", "Behind the Green Door (1972).mp4")))
        self.assertEqual(self.row()["tmdb_status"], "needs_match")

    def test_match_only_cleans_an_already_doubled_title(self):
        self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(title="Behind the Green Door", year=1972))
        d = os.path.join(self.cr, "Behind the Green Door (1972) (1972)"); os.rename(os.path.join(self.cr, "Behind the Green Door (1972)"), d)
        os.rename(os.path.join(d, "Behind the Green Door (1972).mp4"), os.path.join(d, "Behind the Green Door (1972) (1972).mp4"))
        self.c.execute("UPDATE films SET title='Behind the Green Door (1972)' WHERE id=?", (self.fid,))
        self.c.execute("UPDATE files SET path=? WHERE film_id=?", (os.path.join(d, "Behind the Green Door (1972) (1972).mp4"), self.fid)); self.c.commit()
        db.set_setting(self.c, "tmdb_key", "k"); self.c.commit()
        with mock.patch.object(A, "search_tmdb_movie_then_tv", return_value=(None, None)):
            j = self.cl.post(f"/api/films/{self.fid}/to-classic", json=dict(match_only=True)).get_json()
        self.assertIn("marked Needs TMDB match", j["match_note"]); self.assertEqual(self.row()["title"], "Behind the Green Door")
        self.assertEqual(self.c.execute("SELECT path FROM files WHERE film_id=?", (self.fid,)).fetchone()[0],
                         os.path.join(self.cr, "Behind the Green Door (1972)", "Behind the Green Door (1972).mp4"))

if __name__ == "__main__": unittest.main()
