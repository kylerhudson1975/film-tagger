"""Studio click: spelling variants, aliases and the <Studio> folder all match; blank studios filled from the folder (add-only)."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class StudioClick(unittest.TestCase):
    def setUp(self):
        t = tempfile.mkdtemp(); self.a = os.path.join(t, "A", "Porn Scenes"); self.b = os.path.join(t, "B", "Porn Scenes")
        c = db.connect(); db.set_setting(c, "scene_roots", [self.a, self.b])
        c.execute("DELETE FROM files"); c.execute("DELETE FROM films"); c.execute("DELETE FROM studio_aliases"); c.commit(); self.c = c
        self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    def scene(self, root, folder, studio, cast="X", ef=None):
        p = os.path.join(root, folder, cast + ".mp4")
        fid = self.c.execute("INSERT INTO films(title,studio,cast_,kind,added_at,edited_fields) VALUES('T',?,?,'scene',?,?)", (studio, cast, time.time(), ef)).lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, p)); self.c.commit(); return fid

    def test_variants_folder_and_both_roots(self):
        self.scene(self.a, "Mommy's Girl", "Mommy's Girl", "Bea"); self.scene(self.a, "Mommy's Girl", "Mommys Girl", "Ada")
        self.scene(self.b, "Mommy's Girl", "Something Else", "Cy"); self.scene(self.b, "MommysGirl", "", "Di")
        self.scene(self.a, "Other", "Other", "Ed")
        r = self.cl.get("/api/films", query_string={"studio": "Mommy's Girl", "sort": "studio", "order": "asc", "limit": 300}).get_json()
        self.assertEqual(r["total"], 4)
        r2 = self.cl.get("/api/films", query_string={"studio": "Mommys Girl", "limit": 300}).get_json()
        self.assertEqual(r2["total"], 4)

    def test_fill_blank_studio_from_folder_add_only(self):
        f1 = self.scene(self.a, "Perv Mom", "", "A"); f2 = self.scene(self.a, "Perv Mom", None, "B", ef='["studio"]')
        f3 = self.scene(self.a, "Perv Mom", "Kept", "C"); f4 = self.scene(self.a, "_No Studio", "", "D")
        self.assertEqual(db.fill_folder_studios(self.c), 1)
        st = lambda i: self.c.execute("SELECT studio FROM films WHERE id=?", (i,)).fetchone()[0]
        self.assertEqual(st(f1), "Perv Mom"); self.assertIn(st(f2), (None, "")); self.assertEqual(st(f3), "Kept"); self.assertIn(st(f4), (None, ""))

if __name__ == "__main__": unittest.main()
