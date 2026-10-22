"""Several scene roots act as ONE 'Porn Scenes' library (filter, picker, counts, studio sort, scan of a not-yet-created
root) and renames never cross drives."""
import os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

def touch(p):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x")

class MultiRoots(unittest.TestCase):
    def setUp(self):
        t = tempfile.mkdtemp(); self.a = os.path.join(t, "A", "Porn Scenes"); self.b = os.path.join(t, "B", "Porn Scenes")
        self.missing = os.path.join(t, "C", "Porn Scenes")
        c = db.connect(); db.set_setting(c, "scene_roots", [self.a, self.b]); db.set_setting(c, "roots", [self.a, self.b])
        c.execute("DELETE FROM files"); c.execute("DELETE FROM films"); c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    def scene(self, root, studio, cast, date="2023-01-02"):
        p = os.path.join(root, studio, f"{cast}.mp4"); touch(p)
        fid = self.c.execute("INSERT INTO films(title,studio,cast_,release_date,kind,added_at) VALUES('T',?,?,?,'scene',?)",
                             (studio, cast, date, time.time())).lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, p)); self.c.commit(); return fid, p

    def test_one_library_filter_and_studio_sort(self):
        self.scene(self.a, "Passion HD", "Maria Kazi"); self.scene(self.b, "Passion HD", "Alina Lopez"); self.scene(self.b, "Zeta", "Bea")
        for lib in (self.a, self.b):
            r = self.cl.get("/api/films", query_string=dict(lib=lib, sort="studio", order="asc", limit=300)).get_json()
            self.assertEqual([f["cast_"] for f in r["films"]], ["Alina Lopez", "Maria Kazi", "Bea"])
        sub = self.cl.get("/api/films", query_string=dict(lib=os.path.join(self.a, "Passion HD"))).get_json()
        self.assertEqual(len(sub["films"]), 2)

    def test_libraries_merges_roots(self):
        self.scene(self.a, "Passion HD", "X"); self.scene(self.b, "Passion HD", "Y"); self.scene(self.b, "Zeta", "Z")
        libs = self.cl.get("/api/libraries").get_json()
        self.assertEqual(len(libs), 1); self.assertEqual(libs[0]["count"], 3)
        self.assertEqual({s["name"]: s["count"] for s in libs[0]["subfolders"]}, {"Passion HD": 2, "Zeta": 1})

    def test_scan_skips_uncreated_scene_root(self):
        db.set_setting(self.c, "scene_roots", [self.a, self.missing]); os.makedirs(self.a, exist_ok=True)
        r = db.scan(self.c, [self.a, self.missing], {}); self.assertEqual(r["errors"], [])
        self.assertFalse(os.path.exists(self.missing))

    def test_rename_stays_in_own_root(self):
        fid, p = self.scene(os.path.join(self.b, "_No Studio"), "", "Jane Doe")
        self.c.execute("UPDATE films SET studio='New S' WHERE id=?", (fid,)); self.c.commit()
        moves, _ = A.plan_scene_rename(self.c, fid)
        self.assertTrue(moves[0]["new"].startswith(os.path.join(self.b, "New S")))

    def test_cross_drive_guard(self):
        self.assertTrue(db.same_drive(r"D:\x\a.mp4", r"d:\y\b.mp4"))
        if os.name == "nt": self.assertFalse(db.same_drive(r"D:\x\a.mp4", r"L:\y\b.mp4"))
        fid, p = self.scene(self.a, "S", "Q")
        with mock.patch.object(db, "same_drive", return_value=False):
            n = A.do_scene_moves(self.c, [dict(file_id=1, old=p, new=p + ".moved.mp4", root=self.a, sidecars=[], gallery=None)])
        self.assertEqual(n, 0); self.assertTrue(os.path.exists(p))

if __name__ == "__main__": unittest.main()
