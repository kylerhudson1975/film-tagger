"""MKV container titles for hard-matched scenes (mkvtag.py + app hooks). mkvpropedit is faked unless it is installed
(then test_real_mkvpropedit also runs it on a tiny generated MKV). No network."""
import os, sys, json, shutil, subprocess, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, mkvtag, app as A

ROW = dict(title="Sunny Afternoon", studio="Example Studio", release_date="2024-05-12", cast_="Jane Doe, Ann Roe")

class Pure(unittest.TestCase):
    def test_title_format(self):
        self.assertEqual(mkvtag.scene_title(ROW), "Example Studio - Sunny Afternoon (2024-05-12)")
        self.assertEqual(mkvtag.scene_title(dict(ROW, studio="", release_date="")), "Sunny Afternoon")
    def test_tags(self):
        d = mkvtag.desired(ROW)
        self.assertEqual(d["tags"], [("TITLE", "Sunny Afternoon"), ("DATE_RELEASED", "2024-05-12"), ("ACTOR", "Jane Doe"),
                                     ("ACTOR", "Ann Roe"), ("ARTIST", "Jane Doe, Ann Roe"), ("PRODUCTION_STUDIO", "Example Studio")])
        x = mkvtag.tags_xml([("TITLE", "A & B <c>")])
        self.assertIn("<String>A &amp; B &lt;c&gt;</String>", x); self.assertIn("<TargetTypeValue>50</TargetTypeValue>", x)
    def test_signature_changes_with_title(self):
        self.assertNotEqual(mkvtag.signature(mkvtag.desired(ROW)), mkvtag.signature(mkvtag.desired(dict(ROW, title="Other"))))

class FakeRun:
    """Records mkvpropedit calls; appends a byte to the file like a header edit could."""
    def __init__(self, rc=0, title_out=None): self.calls = []; self.rc = rc; self.title_out = title_out
    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if os.path.basename(cmd[0]).startswith("mkvmerge"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"container": {"properties": {"title": self.title_out}}}).encode(), b"")
        if self.rc in (0, 1):
            with open(cmd[1], "ab") as fh: fh.write(b"!")
        return subprocess.CompletedProcess(cmd, self.rc, b"Error: boom" if self.rc == 2 else b"", b"")

class Hooks(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [self.root])
        c.execute("DELETE FROM films WHERE kind='scene'"); mkvtag.ensure_table(c); c.execute("DELETE FROM mkv_meta"); c.commit(); self.c = c
        mkvtag._TOOL.clear(); mkvtag._TOOL.update(mkvpropedit="/fake/mkvpropedit", mkvmerge="/fake/mkvmerge")
        self.run = FakeRun(); self.p1 = mock.patch.object(mkvtag.subprocess, "run", self.run); self.p1.start()
    def tearDown(self): self.p1.stop(); mkvtag.reset(); self.c.close()
    def scene(self, ext=".mkv", notes=None, tid="t1", std=True, **kw):
        f = dict(ROW, **kw); name = A.scene_file_name(f) + ext if std else "raw name" + ext
        p = os.path.join(self.root, f["studio"], name); os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x" * 100)
        mt = time.time() - 86400; os.utime(p, (mt, mt))
        fid = self.c.execute("INSERT INTO films(title,studio,release_date,cast_,kind,notes,tpdb_id,added_at) VALUES(?,?,?,?,?,?,?,?)",
                             (f["title"], f["studio"], f["release_date"], f["cast_"], "scene", notes, tid, time.time())).lastrowid
        self.c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,?)", (fid, p, 100)); self.c.commit(); return fid, p
    def writes(self): return [x for x in self.run.calls if "--edit" in x]

    def test_rename_writes_title_and_keeps_mtime(self):
        fid, p = self.scene(); mt = os.path.getmtime(p)
        n, note, mkv = A.rename_scene_file(self.c, fid)
        self.assertEqual(mkv["status"], "updated"); cmd = self.writes()[0]
        self.assertEqual(cmd[2:5], ["--edit", "info", "--set"]); self.assertEqual(cmd[5], "title=Example Studio - Sunny Afternoon (2024-05-12)")
        self.assertTrue(cmd[7].startswith("global:")); self.assertAlmostEqual(os.path.getmtime(p), mt, delta=1)
        self.assertEqual(self.c.execute("SELECT size FROM files WHERE film_id=?", (fid,)).fetchone()[0], 101)  # size refreshed
        A.rename_scene_file(self.c, fid); self.assertEqual(len(self.writes()), 1)  # unchanged: not written again

    def test_hand_edited_title_used(self):
        fid, p = self.scene(); A.rename_scene_file(self.c, fid)
        self.run.title_out = "Example Studio - Sunny Afternoon (2024-05-12)"  # file still has what we wrote
        self.c.execute("UPDATE films SET title='My Own Title' WHERE id=?", (fid,)); self.c.commit()
        n, note, mkv = A.rename_scene_file(self.c, fid)  # Rename on Save moves the file, then writes the edited title
        self.assertEqual(mkv["status"], "updated"); self.assertEqual(self.writes()[-1][5], "title=Example Studio - My Own Title (2024-05-12)")

    def test_title_edited_inside_file_is_kept(self):
        fid, p = self.scene(); A.rename_scene_file(self.c, fid)
        self.run.title_out = "Kyle typed this"
        self.c.execute("UPDATE films SET cast_='Jane Doe' WHERE id=?", (fid,)); self.c.commit()
        mkv = A.rename_scene_file(self.c, fid)[2]
        self.assertEqual(mkv["status"], "skipped"); self.assertEqual(len(self.writes()), 1)

    def test_skips_review_soft_mp4_unmatched(self):
        r, _ = self.scene(notes="Review by hand", title="R1"); s, _ = self.scene(std=False, title="S1")
        m, _ = self.scene(ext=".mp4", title="M1"); u, _ = self.scene(tid=None, title="U1")
        for fid in (r, m, u): self.assertIsNone(A.mkv_after_rename(self.c, fid))
        self.assertIsNone(A.mkv_after_rename(self.c, s)); self.assertEqual(self.writes(), [])
        d = A.app.test_client().get("/api/mkv/titles/preview").get_json()
        self.assertEqual((d["eligible"], d["would_update"], d["skipped"]["review"], d["skipped"]["not_renamed"], d["skipped"]["not_matched"]), (0, 0, 1, 1, 1))

    def test_failure_never_breaks_match(self):
        fid, p = self.scene(); self.run.rc = 2
        n, note, mkv = A.rename_scene_file(self.c, fid)
        self.assertEqual(mkv["status"], "failed"); self.assertIn("boom", mkv["note"]); self.assertIsNone(note)
        with mock.patch.object(mkvtag, "write", side_effect=RuntimeError("x")):
            self.assertEqual(A.mkv_after_rename(self.c, fid)["status"], "failed")
        with mock.patch.object(A, "mkv_plan", side_effect=RuntimeError("x")):
            self.assertEqual(A.mkv_after_rename(self.c, fid)["status"], "failed")

    def test_in_use_skipped(self):
        fid, p = self.scene()
        with mock.patch.object(mkvtag, "in_use", return_value=True): mkv = A.rename_scene_file(self.c, fid)[2]
        self.assertEqual(mkv["status"], "skipped"); self.assertEqual(self.writes(), [])

    def test_missing_tool_disabled(self):
        fid, p = self.scene(); mkvtag._TOOL.update(mkvpropedit=None)
        self.assertIsNone(A.rename_scene_file(self.c, fid)[2])
        cl = A.app.test_client(); self.assertEqual(cl.post("/api/mkv/titles/start", json={}).status_code, 400)
        self.assertFalse(cl.get("/api/mkv/titles/preview").get_json()["available"])

    def test_bulk_pass_dry_run_then_live(self):
        a, _ = self.scene(title="A1"); b, _ = self.scene(title="B1"); self.scene(notes="Review by hand", title="R1")
        cl = A.app.test_client(); d = cl.get("/api/mkv/titles/preview").get_json()
        self.assertEqual((d["eligible"], d["would_update"], d["already"], d["skipped"]["review"]), (2, 2, 0, 1)); self.assertEqual(self.writes(), [])
        self.assertEqual(cl.post("/api/mkv/titles/start", json={}).status_code, 200)
        for _ in range(100):
            j = cl.get("/api/mkv/titles/status").get_json()
            if not j["running"]: break
            time.sleep(0.05)
        self.assertEqual((j["total"], j["updated"], j["failed"]), (2, 2, 0))
        d = cl.get("/api/mkv/titles/preview").get_json(); self.assertEqual((d["would_update"], d["already"]), (0, 2))

    def test_bulk_pass_one_film(self):
        a, _ = self.scene(title="A1"); b, _ = self.scene(title="B1"); cl = A.app.test_client()
        self.assertEqual(cl.post("/api/mkv/titles/start", json={"film_ids": [b]}).status_code, 200)
        for _ in range(100):
            j = cl.get("/api/mkv/titles/status").get_json()
            if not j["running"]: break
            time.sleep(0.05)
        self.assertEqual((j["total"], j["updated"]), (1, 1)); self.assertIn("B1", self.writes()[0][5])

    def test_tpdb_apply_reports_note(self):
        fid, p = self.scene(std=False, title="Raw")
        sc = {"id": "t1", "title": "Sunny Afternoon", "date": "2024-05-12", "site": {"name": "Example Studio"}, "performers": [{"name": "Jane Doe"}]}
        db.set_setting(self.c, "tpdb_key", "tok"); self.c.commit(); self.run.rc = 2
        with mock.patch.object(A.tpdb, "get_scene", lambda tok, sid: sc), mock.patch.object(A, "kick_focus", lambda: None):
            r = A.app.test_client().post(f"/api/films/{fid}/tpdb", json={"tpdb_id": "t1"})
        db.set_setting(self.c, "tpdb_key", ""); self.c.commit()
        self.assertEqual(r.status_code, 200); j = r.get_json(); self.assertEqual(j["mkv_status"], "failed")
        self.assertTrue(j["files"][0]["path"].endswith(".mkv") and "Raw" not in j["files"][0]["path"])  # rename still happened

@unittest.skipUnless(shutil.which("mkvpropedit") and shutil.which("mkvmerge") and shutil.which("ffmpeg"), "MKVToolNix/ffmpeg not installed")
class Real(unittest.TestCase):
    def test_real_mkvpropedit(self):
        mkvtag.reset(); d = tempfile.mkdtemp(); p = os.path.join(d, "t.mkv")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=64x64:d=1", "-c:v", "ffv1", p], check=True)
        ok, msg = mkvtag.write(p, mkvtag.desired(ROW)); self.assertTrue(ok, msg)
        self.assertEqual(mkvtag.read_title(p), "Example Studio - Sunny Afternoon (2024-05-12)")
        j = json.loads(subprocess.run(["mkvmerge", "-J", p], capture_output=True).stdout)
        self.assertGreaterEqual(j.get("global_tags", [{}])[0].get("num_entries", 0), 6)

if __name__ == "__main__": unittest.main()
