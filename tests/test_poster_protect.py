"""Uploaded posters are protected for good: an upload / URL / gallery poster is marked hand-edited, a replaced or removed
custom poster file is moved to posters/backup/ (never deleted) and can be restored, TMDB and ThePornDB refreshes
(background or explicit Load) never change it, and merging keeps it. posterrecover.py finds an older library's posters
by video file and links only the certain, non-adult ones. Temp DB, no network. Run: python -m pytest tests/test_poster_protect.py"""
import io, json, os, sqlite3, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A, posterrecover as PR

JPG = b"\xff\xd8\xff\xe0" + b"x" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"y" * 200

def local(v): return os.path.join(A.POSTERS_DIR, os.path.basename(v))
def backup(v): return os.path.join(A.POSTER_BACKUP_DIR, os.path.basename(v))

class Protect(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(); self.cl = A.app.test_client()
        self.fid = self.c.execute("INSERT INTO films(title,kind,tmdb_id,poster,added_at) VALUES('Cocoon','movie',10328,'https://image.tmdb.org/t/p/w342/tmdb.jpg',?)", (time.time(),)).lastrowid
        self.c.commit()
        self.p = [mock.patch.object(A, "kick_focus", lambda: None)]
        for x in self.p: x.start()
    def tearDown(self):
        for x in self.p: x.stop()
    def row(self, fid=None): return dict(self.c.execute("SELECT * FROM films WHERE id=?", (fid or self.fid,)).fetchone())
    def upload(self, data=JPG, name="p.jpg", mt="image/jpeg", fid=None):
        r = self.cl.post(f"/api/films/{fid or self.fid}/poster", data={"file": (io.BytesIO(data), name, mt)}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_json()); return r.get_json()["custom_poster"]

    def test_upload_is_marked_hand_edited(self):
        v = self.upload(); f = self.row()
        self.assertEqual(f["custom_poster"], v); self.assertTrue(os.path.isfile(local(v)))
        self.assertIn("custom_poster", json.loads(f["edited_fields"]))

    def test_reupload_backs_up_never_deletes_and_restores(self):
        v1 = self.upload(); time.sleep(0.01); v2 = self.upload(PNG, "q.png", "image/png")
        self.assertNotEqual(v1, v2); self.assertTrue(os.path.isfile(local(v2)))
        self.assertTrue(os.path.isfile(backup(v1)), "the first upload must be kept in posters/backup")
        r = self.cl.post(f"/api/films/{self.fid}/findposters/restore").get_json()
        self.assertEqual(r["custom_poster"], v1); self.assertTrue(os.path.isfile(local(v1)))
        self.assertTrue(os.path.isfile(backup(v2))); self.assertIn("custom_poster", json.loads(self.row()["edited_fields"]))

    def test_same_second_uploads_never_overwrite(self):
        with mock.patch.object(A.time, "time", lambda: 1793310000.5):
            v1 = self.upload(); v2 = self.upload(PNG, "q.png", "image/png")
        self.assertNotEqual(os.path.basename(v1), os.path.basename(v2))
        with open(backup(v1), "rb") as fh: self.assertEqual(fh.read(), JPG)

    def test_remove_keeps_the_file_in_backup(self):
        v = self.upload()
        self.cl.delete(f"/api/films/{self.fid}/poster")
        f = self.row(); self.assertIsNone(f["custom_poster"]); self.assertNotIn("custom_poster", json.loads(f["edited_fields"]))
        self.assertFalse(os.path.exists(local(v))); self.assertTrue(os.path.isfile(backup(v)))
        self.assertEqual(self.cl.post(f"/api/films/{self.fid}/findposters/restore").get_json()["custom_poster"], v)

    def test_tmdb_refresh_never_touches_it(self):
        v = self.upload()
        m = {"id": 10328, "title": "Cocoon", "release_date": "1985-06-21", "poster_path": "/new.jpg", "credits": {}, "genres": []}
        A.apply_movie(self.c, self.fid, m); self.c.commit()
        f = self.row(); self.assertEqual(f["custom_poster"], v); self.assertTrue(f["poster"].endswith("/new.jpg"))

    def test_tpdb_load_and_background_never_touch_it(self):
        sid = self.c.execute("INSERT INTO films(title,kind,added_at) VALUES('s','scene',?)", (time.time(),)).lastrowid; self.c.commit()
        v = self.upload(fid=sid)
        sc = {"id": "u1", "title": "T", "poster": "http://x/p.jpg", "background": {"large": "http://x/w.jpg"}, "tags": []}
        for override in (False, True):
            with mock.patch.object(A.tpdb, "scene_fields", lambda s: dict(title="T", poster="http://x/p.jpg", custom_poster="http://evil/x.jpg", tags=[])):
                A.apply_scene(self.c, sid, sc, override=override); self.c.commit()
            f = self.row(sid); self.assertEqual(f["custom_poster"], v); self.assertIn("custom_poster", json.loads(f["edited_fields"]))

    def test_merge_keeps_uploaded_poster(self):
        other = self.c.execute("INSERT INTO films(title,kind,added_at) VALUES('Cocoon dup','movie',?)", (time.time(),)).lastrowid; self.c.commit()
        v = self.upload(fid=other)
        self.cl.post("/api/films/merge", json={"ids": [self.fid, other]})
        f = self.row(); self.assertEqual(f["custom_poster"], v); self.assertTrue(os.path.isfile(local(v)))

    def test_save_cannot_clear_it(self):
        v = self.upload()
        self.cl.put(f"/api/films/{self.fid}", json={"title": "Cocoon", "custom_poster": None, "poster": ""})
        self.assertEqual(self.row()["custom_poster"], v)


class Recover(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(); self.tmp = tempfile.mkdtemp()
        self.old = os.path.join(self.tmp, "films.db"); op = os.path.join(self.tmp, "posters"); os.makedirs(op)
        o = sqlite3.connect(self.old)
        o.executescript("CREATE TABLE films(id INTEGER PRIMARY KEY, title TEXT, year INT, kind TEXT, primary_genre TEXT, tmdb_id INT, custom_poster TEXT);"
                        "CREATE TABLE files(id INTEGER PRIMARY KEY, film_id INT, path TEXT);")
        u = str(time.time_ns())
        def old(oid, title, year, kind, genre, tmdb, path, has_file=True):
            o.execute("INSERT INTO films VALUES(?,?,?,?,?,?,?)", (oid, title, year, kind, genre, tmdb, f"/posters/film-{oid}-1.jpg"))
            o.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (oid, path))
            if has_file: open(os.path.join(op, f"film-{oid}-1.jpg"), "wb").write(JPG)
        def new(title, year, kind, genre, tmdb, path, cp=None, notes=None):
            fid = self.c.execute("INSERT INTO films(title,year,kind,primary_genre,tmdb_id,custom_poster,notes,added_at) VALUES(?,?,?,?,?,?,?,?)",
                                 (title, year, kind, genre, tmdb, cp, notes, time.time())).lastrowid
            self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path)); return fid
        P = lambda n: f"D:\\Films{u}\\{n}.mkv"
        old(1, "Old Name", 1984, None, "Drama", 19400, P("a")); self.safe = new("New Name", 1984, None, "Drama", 19400, P("A"))
        old(2, "Adult", 1980, None, "Hardcore", 5, P("b")); self.adult = new("Adult", 1980, None, "Hardcore", 5, P("b"))
        old(3, "Has one", 1990, None, "Horror", 6, P("c")); self.has = new("Has one", 1990, None, "Horror", 6, P("c"), cp="/posters/mine.jpg")
        old(4, "Other film", 1991, None, "Horror", 7, P("d")); new("Different", 1991, None, "Horror", 8, P("d"))
        old(5, "Gone file", 1992, None, "Horror", 9, P("e"), has_file=False); new("Gone file", 1992, None, "Horror", 9, P("e"))
        old(6, "Scene", 2020, "scene", "Hardcore", None, P("f")); self.rbh = new("Scene", 2020, "scene", "Hardcore", None, P("f"), notes="Review by hand")
        o.commit(); o.close(); self.c.commit()

    def test_plan_and_apply_only_safe(self):
        items = {it["old_id"]: it for it in PR.plan(self.old, self.c)}
        self.assertEqual([k for k, it in items.items() if it["safe"]], [1])
        self.assertEqual(items[2]["status"], "adult: listed only, link by hand")
        self.assertEqual(items[3]["status"], "current film already has a custom poster")
        self.assertEqual(items[4]["status"], "same file but a different TMDB id / title")
        self.assertEqual(items[5]["status"], "old poster file missing")
        self.assertEqual(items[6]["status"], "Review by hand: left alone")
        self.assertEqual(PR.apply(list(items.values()), self.c, A.POSTERS_DIR), 1)
        f = dict(self.c.execute("SELECT * FROM films WHERE id=?", (self.safe,)).fetchone())
        self.assertTrue(os.path.isfile(local(f["custom_poster"]))); self.assertIn("custom_poster", json.loads(f["edited_fields"]))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "posters", "film-1-1.jpg")), "the old file is copied, never moved")
        for fid in (self.adult, self.rbh): self.assertIsNone(self.c.execute("SELECT custom_poster FROM films WHERE id=?", (fid,)).fetchone()[0])
        self.assertEqual(self.c.execute("SELECT custom_poster FROM films WHERE id=?", (self.has,)).fetchone()[0], "/posters/mine.jpg")

if __name__ == "__main__": unittest.main()
