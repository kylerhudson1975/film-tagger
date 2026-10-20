"""Rename on Save + Fix all file names for scenes: standard name, sidecars, collisions, junk double extensions,
missing fields skipped, DB path updated, CSV log, empty old folder removed but _No Studio kept."""
import csv, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

def touch(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data)

class SceneRename(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [self.root])
        c.execute("DELETE FROM films WHERE kind='scene'"); c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    def scene(self, rel, **f):
        p = os.path.join(self.root, rel); touch(p)
        cols = dict(title="T", studio=None, release_date=None, cast_=None, kind="scene", added_at=time.time()); cols.update(f)
        fid = self.c.execute(f"INSERT INTO films({','.join(cols)}) VALUES({','.join('?'*len(cols))})", list(cols.values())).lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, p)); self.c.commit(); return fid, p
    path = lambda self, fid: self.c.execute("SELECT path FROM files WHERE film_id=?", (fid,)).fetchone()[0]

    def test_save_renames_with_sidecar_and_junk_ext(self):
        fid, p = self.scene(os.path.join("Old Studio", "clip.mp4 - EPORN.mp4"), studio="Old Studio", release_date="2023-01-02", title="A")
        touch(p[:-4] + ".srt")
        r = self.cl.put(f"/api/films/{fid}", json={"studio": "New: Studio?", "cast_": "Jane Doe, John Roe", "title": "Big/Day"}).get_json()
        exp = os.path.join(self.root, "New Studio", "New Studio - 2023-01-02 - Jane Doe, John Roe - BigDay.mp4")
        self.assertEqual(self.path(fid), exp); self.assertTrue(os.path.exists(exp)); self.assertTrue(os.path.exists(exp[:-4] + ".srt"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "Old Studio")))  # empty old folder removed
        self.assertNotIn("rename_note", r)
        rows = list(csv.reader(open(os.path.join(self.root, "_filmtagger_renames.csv"), encoding="utf-8-sig")))
        self.assertEqual(rows[0], [p, exp])

    def test_missing_fields_skipped_with_note(self):
        fid, p = self.scene(os.path.join("S", "x.mp4"), studio="S", title="A")
        r = self.cl.put(f"/api/films/{fid}", json={"title": "B"}).get_json()
        self.assertEqual(self.path(fid), p); self.assertIn("missing date", r["rename_note"])

    def test_collision_appends_2_and_keeps_no_studio(self):
        tgt = os.path.join(self.root, "S", "S - 2020-01-01 - P - T.mp4"); touch(tgt, b"other")
        fid, p = self.scene(os.path.join("_No Studio", "y.mp4"), studio="S", release_date="2020-01-01", cast_="P", title="X")
        self.cl.put(f"/api/films/{fid}", json={"title": "T"})
        self.assertEqual(self.path(fid), tgt[:-4] + " (2).mp4"); self.assertEqual(open(tgt, "rb").read(), b"other")
        self.assertTrue(os.path.isdir(os.path.join(self.root, "_No Studio")))

    def test_non_name_field_does_not_rename(self):
        fid, p = self.scene(os.path.join("S", "z.mp4"), studio="S", release_date="2020-01-01", title="T")
        self.cl.put(f"/api/films/{fid}", json={"notes": "hi"}); self.assertEqual(self.path(fid), p)

    def test_fix_all_preview_then_apply(self):
        a, pa = self.scene(os.path.join("S", "a.mp4"), studio="S", release_date="2021-05-06", cast_="Ann", title="One")
        b, pb = self.scene(os.path.join("S", "S - 2021-05-06 - Bob - Two.mp4"), studio="S", release_date="2021-05-06", cast_="Bob", title="Two")
        s, ps = self.scene(os.path.join("S", "c.mp4"), studio="S", title="NoDate")
        j = self.cl.get("/api/scenes/rename-preview").get_json()
        self.assertEqual(j["count"], 1); self.assertEqual(j["skipped"], 1); self.assertEqual(j["items"][0]["old"], pa)
        self.assertTrue(os.path.exists(pa))  # preview moves nothing
        self.assertEqual(self.cl.post("/api/scenes/rename-apply", json={}).get_json()["renamed"], 1)
        self.assertEqual(self.path(a), os.path.join(self.root, "S", "S - 2021-05-06 - Ann - One.mp4"))
        self.assertEqual((self.path(b), self.path(s)), (pb, ps))
        self.assertEqual(self.cl.get("/api/scenes/rename-preview").get_json()["count"], 0)

    def test_hold_rename_note_skips_fix_all(self):
        a, pa = self.scene(os.path.join("S", "h.mp4"), studio="S", release_date="2021-05-06", cast_="Ann", title="Held", notes="[Hold rename] shaky match")
        j = self.cl.get("/api/scenes/rename-preview").get_json(); self.assertEqual((j["count"], j["held"]), (0, 1))
        self.cl.post("/api/scenes/rename-apply", json={}); self.assertEqual(self.path(a), pa)

if __name__ == "__main__": unittest.main()
