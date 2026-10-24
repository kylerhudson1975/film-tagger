"""Auto-rename after a ThePornDB scan only at extremely high confidence (fingerprint, or studio+date+full title+all
performers with no close runner-up); everything else gets details only and is flagged 'rename waiting'. No network."""
import os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, tpdb, app as A

SC = {"id": "s1", "title": "Perfect Moment", "date": "2022-12-04", "site": {"name": "Girls Only Porn"},
      "performers": [{"name": "Alexis Crystal"}, {"name": "Agatha Vega"}]}
TWIN = dict(SC, id="s2", title="Perfect Moment Two")  # a different scene that scores about the same
STD = "Girls Only Porn - 2022-12-04 - Alexis Crystal, Agatha Vega - Perfect Moment.mp4"

def fake_fetch(results, hash_hit=None):
    def f(path, **q):
        if path == "/sites": return {"data": [{"name": "Girls Only Porn"}]}
        if "hash" in q: return {"data": [hash_hit] if hash_hit else []}
        return {"data": results}
    return f

class AutoRename(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [self.root])
        c.execute("DELETE FROM films WHERE kind='scene'"); c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    def scene(self, name, notes=None, size=10):
        p = os.path.join(self.root, "Girls Only Porn", name); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(b"x" * size)
        fid = self.c.execute("INSERT INTO films(title,kind,notes,added_at) VALUES(?,?,?,?)", (os.path.splitext(name)[0], "scene", notes, time.time())).lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, p)); self.c.commit(); return fid, p
    def run_job(self, fid, results, hash_hit=None):
        real = tpdb.identify
        with mock.patch.object(A.tpdb, "identify", lambda tok, path: real(tok, path, fetch=fake_fetch(results, hash_hit))), \
             mock.patch.object(A.tpdb, "get_scene", lambda tok, sid: {"s1": SC, "s2": TWIN}.get(sid)), \
             mock.patch.object(A, "kick_focus", lambda: None), mock.patch.object(A.tpdb, "call_retry", side_effect=AssertionError("network")):
            A.TP.update(cancel=False, matched=0, skipped=0, errors=0, done=0, running=True); A.run_tpdb_job("tok", [fid])
        self.c = db.connect()
        r = self.c.execute("SELECT f.path, m.title, m.tpdb_id, COALESCE(m.rename_pending,0) FROM files f JOIN films m ON m.id=f.film_id WHERE m.id=?", (fid,)).fetchone()
        return os.path.basename(r[0]), r[1], r[2], r[3]

    def test_fingerprint_renamed(self):
        fid, p = self.scene("random junk name.mp4", size=200000)
        name, title, tid, pend = self.run_job(fid, [], hash_hit=SC)
        self.assertEqual((name, title, tid, pend), (STD, "Perfect Moment", "s1", 0))

    def test_full_agreement_renamed(self):
        fid, p = self.scene("GirlsOnlyPorn 2022-12-04 Alexis Crystal Agatha Vega Perfect Moment.mp4")
        name, title, tid, pend = self.run_job(fid, [SC])
        self.assertEqual((name, tid, pend), (STD, "s1", 0))

    def test_partial_title_flagged(self):
        sc = dict(SC, title="Perfect Moment Bliss")
        fid, p = self.scene("GirlsOnlyPorn 2022-12-04 Alexis Crystal Agatha Vega Perfect Moment Sunset.mp4")
        with mock.patch.object(A.tpdb, "get_scene", lambda tok, sid: sc):
            real = tpdb.identify
            with mock.patch.object(A.tpdb, "identify", lambda tok, path: real(tok, path, fetch=fake_fetch([sc]))), mock.patch.object(A, "kick_focus", lambda: None):
                A.TP.update(cancel=False, matched=0, skipped=0, errors=0, done=0); A.run_tpdb_job("tok", [fid])
        c = db.connect(); r = c.execute("SELECT title, tpdb_id, rename_pending FROM films WHERE id=?", (fid,)).fetchone()
        self.assertEqual(tuple(r), ("Perfect Moment Bliss", "s1", 1))  # details applied, not renamed
        self.assertTrue(os.path.exists(p))
        d = self.cl.get("/api/scenes/rename-waiting").get_json()
        self.assertEqual(d["flagged"], 1); self.assertEqual(d["items"][0]["old_name"], os.path.basename(p))
        self.assertTrue(d["items"][0]["new_name"].endswith("Perfect Moment Bliss.mp4"))
        self.cl.post("/api/scenes/rename-apply", json={"ids": [fid]})
        self.assertEqual(c.execute("SELECT rename_pending FROM films WHERE id=?", (fid,)).fetchone()[0], 0)
        self.assertEqual(self.cl.get("/api/scenes/rename-waiting").get_json()["count"], 0)

    def test_held_and_review_not_renamed(self):
        for notes in ("[Hold rename]", "Review by hand"):
            fid, p = self.scene(f"GirlsOnlyPorn 2022-12-04 Alexis Crystal Agatha Vega Perfect Moment {len(notes)}.mp4", notes=notes)
            name, title, tid, pend = self.run_job(fid, [SC])
            self.assertEqual(name, os.path.basename(p)); self.assertEqual(tid, "s1"); self.assertEqual(pend, 0)
            self.assertEqual(self.c.execute("SELECT COUNT(*) FROM films WHERE id=? AND rename_pending=1", (fid,)).fetchone()[0], 0)
        self.assertEqual(self.cl.get("/api/scenes/rename-waiting").get_json()["count"], 0)  # never listed

    def test_runner_up_close_not_renamed(self):
        fid, p = self.scene("GirlsOnlyPorn 2022-12-04 Alexis Crystal Agatha Vega Perfect Moment.mp4")
        r = tpdb.identify("tok", p, fetch=fake_fetch([SC, TWIN]))
        self.assertGreaterEqual(r["runner_up"], r["score"] - 1); self.assertFalse(tpdb.rename_safe(r))
        name, title, tid, pend = self.run_job(fid, [SC, TWIN])
        self.assertEqual(name, os.path.basename(p)); self.assertEqual(pend, 1)

    def test_old_matched_listed(self):
        fid, p = self.scene("old name.mp4")
        self.c.execute("UPDATE films SET tpdb_id='s9', studio='Girls Only Porn', release_date='2020-01-01', title='Old' WHERE id=?", (fid,)); self.c.commit()
        d = self.cl.get("/api/scenes/rename-waiting").get_json()
        self.assertEqual([x["id"] for x in d["items"]], [fid]); self.assertFalse(d["items"][0]["flagged"]); self.assertTrue(os.path.exists(p))

class SoftMatch(AutoRename):
    def soft(self, fid, **q):
        d = self.cl.get("/api/films", query_string=dict(limit=20000, **q)).get_json()
        return {x["id"]: x["soft_match"] for x in d["films"]}.get(fid)
    def test_soft_flag_and_filter(self):
        fid, p = self.scene("old name.mp4")
        self.c.execute("UPDATE films SET tpdb_id='s9', studio='Girls Only Porn', release_date='2020-01-01', title='Old', poster='/p.jpg' WHERE id=?", (fid,)); self.c.commit()
        std, q = self.scene("plain.mp4"); self.assertFalse(self.soft(std))  # unmatched: no flag
        self.assertTrue(self.soft(fid)); self.assertTrue(self.soft(fid, soft_match=1)); self.assertIsNone(self.soft(std, soft_match=1))
        it = self.cl.get("/api/scenes/rename-waiting").get_json()["items"][0]
        self.assertEqual((it["title"], it["studio"], it["date"], it["thumb"]), ("Old", "Girls Only Porn", "2020-01-01", "/p.jpg"))
        self.cl.post("/api/scenes/rename-apply", json={"ids": [fid]}); self.assertFalse(self.soft(fid))  # cleared once renamed
    def test_soft_not_for_review_or_held(self):
        for n in ("Review by hand", "[Hold rename]"):
            fid, p = self.scene(f"x{len(n)}.mp4", notes=n)
            self.c.execute("UPDATE films SET tpdb_id='s9', rename_pending=1, studio='S', release_date='2020-01-01', title='T' WHERE id=?", (fid,)); self.c.commit()
            self.assertFalse(self.soft(fid))
    def test_auto_renamed_not_soft(self):
        fid, p = self.scene("GirlsOnlyPorn 2022-12-04 Alexis Crystal Agatha Vega Perfect Moment.mp4")
        self.run_job(fid, [SC]); self.assertFalse(self.soft(fid))

if __name__ == "__main__": unittest.main()
