"""Explicit Load of a ThePornDB id replaces hand-locked fields (except this session's edits and a hand-edited poster) and
unlocks them; background matching still never touches locked fields. No network."""
import json, os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

SC = {"id": "u9", "title": "Real Title", "date": "2020-10-10", "site": {"name": "Mom XXX"}, "description": "Real plot",
      "poster": "http://x/p.jpg", "performers": [{"name": "Jane Doe"}, {"name": "Ann Roe"}], "tags": []}

class Load(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(); db.set_setting(self.c, "tpdb_key", "t"); db.set_setting(self.c, "scene_roots", [tempfile.mkdtemp()])
        self.fid = self.c.execute("INSERT INTO films(title,cast_,plot,poster,kind,added_at,edited_fields) VALUES(?,?,?,?,?,?,?)",
            ("junk", "junk cast", "junk plot", "/hand.jpg", "scene", time.time(), json.dumps(["cast_", "plot", "poster", "release_date", "title", "tpdb_id", "year"]))).lastrowid
        self.c.commit(); self.cl = A.app.test_client()
        self.p = [mock.patch.object(A.tpdb, "get_scene", lambda tok, sid: SC), mock.patch.object(A, "kick_focus", lambda: None)]
        for x in self.p: x.start()
    def tearDown(self):
        for x in self.p: x.stop()
    def row(self): return dict(self.c.execute("SELECT * FROM films WHERE id=?", (self.fid,)).fetchone())

    def test_explicit_load_overrides_and_unlocks(self):
        r = self.cl.post(f"/api/films/{self.fid}/tpdb", json={"tpdb_id": "u9", "rename": False}); self.assertEqual(r.status_code, 200)
        f = self.row()
        self.assertEqual((f["title"], f["cast_"], f["plot"], f["release_date"], f["year"], f["studio"]), ("Real Title", "Jane Doe, Ann Roe", "Real plot", "2020-10-10", 2020, "Mom XXX"))
        self.assertEqual(f["poster"], "/hand.jpg")  # hand-edited poster stays
        self.assertEqual(json.loads(f["edited_fields"]), ["poster"])

    def test_same_session_edits_kept(self):
        self.cl.post(f"/api/films/{self.fid}/tpdb", json={"tpdb_id": "u9", "rename": False, "keep": ["title"]})
        f = self.row(); self.assertEqual(f["title"], "junk"); self.assertEqual(f["plot"], "Real plot")
        self.assertIn("title", json.loads(f["edited_fields"])); self.assertNotIn("plot", json.loads(f["edited_fields"]))

    def test_background_job_respects_locks(self):
        with mock.patch.object(A.tpdb, "identify", lambda tok, p: dict(scene=SC, score=20, why=["studio"], strategy="parse", confident=True)):
            self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (self.fid, "/nowhere/x.mp4")); self.c.commit()
            A.TP.update(cancel=False, matched=0, skipped=0, errors=0, done=0); A.run_tpdb_job("t", [self.fid])
        self.c = db.connect(); f = self.row()
        self.assertEqual((f["title"], f["cast_"], f["plot"], f["poster"]), ("junk", "junk cast", "junk plot", "/hand.jpg"))
        self.assertEqual(f["studio"], "Mom XXX")  # unlocked field filled
        self.assertEqual(len(json.loads(f["edited_fields"])), 7)

if __name__ == "__main__": unittest.main()
