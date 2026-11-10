"""Stuck soft match: the scene's file was replaced/renamed outside the app (e.g. re-encoded) and already sits at its
standard name, while the library still points at the old, now-missing path. Save / Load / Fix names relink it so the
orange soft-match flag clears; nothing is relinked when the target is ambiguous or claimed. Run: python tests/test_soft_match_relink.py"""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

def touch(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data)

class SoftRelink(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [self.root])
        c.execute("DELETE FROM films"); c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    def stuck(self, std_ext=".mkv"):
        old = os.path.join(self.root, "Naughty Bookworms", "Naughty Bookworms - 2015-02-23 - Molly Jane - Desk.mkv")
        fid = self.c.execute("INSERT INTO films(title,studio,release_date,cast_,kind,tpdb_id,added_at) VALUES('Desk','Naughty Bookworms','2015-02-23','Molly Jane, Preston Parker','scene','t1',?)", (time.time(),)).lastrowid
        self.c.execute("INSERT INTO files(film_id,path,size,missing) VALUES(?,?,100,1)", (fid, old)); self.c.commit()
        new = os.path.join(self.root, "Naughty Bookworms", "Naughty Bookworms - 2015-02-23 - Molly Jane, Preston Parker - Desk" + std_ext)
        touch(new, b"reencoded"); return fid, old, new
    def soft(self, fid): return fid in A.soft_match_ids(self.c)
    def row(self, fid): return tuple(self.c.execute("SELECT path, missing, size FROM files WHERE film_id=?", (fid,)).fetchone())

    def test_save_clears_stuck_soft_match(self):
        fid, old, new = self.stuck()
        self.assertTrue(self.soft(fid))
        self.cl.put(f"/api/films/{fid}", json={"notes": ""})       # plain Save, no name fields changed
        self.assertEqual(self.row(fid), (new, 0, 9)); self.assertFalse(self.soft(fid))
        self.assertTrue(self.cl.get(f"/api/films/{fid}").get_json()["files"][0]["path"] == new)

    def test_fix_names_path_relinks_other_extension(self):
        fid, old, new = self.stuck(".mp4")                         # re-encode changed the container
        A.rename_scene_file(self.c, fid)
        self.assertEqual(self.row(fid)[:2], (new, 0)); self.assertFalse(self.soft(fid))

    def test_claimed_target_not_relinked(self):
        fid, old, new = self.stuck()
        other = self.c.execute("INSERT INTO films(title,kind) VALUES('dup','scene')").lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (other, new)); self.c.commit()
        A.rename_scene_file(self.c, fid)
        self.assertEqual(self.row(fid)[0], old); self.assertTrue(self.soft(fid))

    def test_two_candidates_not_relinked(self):
        fid, old, new = self.stuck(); touch(new[:-4] + ".mp4")
        A.rename_scene_file(self.c, fid)
        self.assertEqual(self.row(fid)[0], old)

    def test_existing_old_file_untouched(self):
        fid, old, new = self.stuck(); touch(old)                   # old file still there: a normal rename case, no relink
        self.assertEqual(A.relink_standard_named(self.c, fid), 0)

if __name__ == "__main__": unittest.main()
