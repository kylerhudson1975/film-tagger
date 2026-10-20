"""Manual (free-entry) primary genre: new names are remembered and listed, hand edits are locked against auto fill /
smart-tag / TMDB, and Classic Porn films accept a manual value while auto still only gives Hardcore/Softcore."""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class ManualPrimaryGenre(unittest.TestCase):
    def setUp(self):
        c = db.connect(); db.get_tag_id(c, "Thriller", "Genre"); db.set_setting(c, "custom_primary_genres", [])
        def film(title, path):
            fid = c.execute("INSERT INTO films(title,added_at) VALUES(?,?)", (title + str(time.time()), time.time())).lastrowid
            c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path.replace(".mkv", f"{fid}.mkv"))); return fid
        self.f = film("Normal", r"D:\Films\n.mkv"); self.cp = film("Classic", r"D:\Classic Porn\x\c.mkv")
        c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    def row(self, i): return self.c.execute("SELECT primary_genre, edited_fields FROM films WHERE id=?", (i,)).fetchone()

    def test_new_value_saved_listed_and_locked(self):
        self.assertEqual(self.cl.put(f"/api/films/{self.f}", json={"primary_genre": "  Giallo  Horror "}).status_code, 200)
        pg, ed = self.row(self.f)
        self.assertEqual(pg, "Giallo Horror"); self.assertIn("primary_genre", json.loads(ed))
        j = self.cl.get("/api/primary-genres").get_json()
        self.assertIn("Giallo Horror", j["choices"]); self.assertIn("Giallo Horror", j["custom"]); self.assertIn("Thriller", j["choices"])
        self.assertIn("Giallo Horror", [u["name"] for u in j["used"]])
        # auto fill / smart-tag never overwrite the hand edit
        self.assertFalse(db.set_primary_genre(self.c, self.f, "Thriller", only_if_empty=False))
        db.import_ai_tags(self.c, [{"film_id": self.f, "primary_genre": "Thriller", "tags": []}], dequeue=False)
        self.assertEqual(self.row(self.f)[0], "Giallo Horror")

    def test_case_insensitive_reuse(self):
        self.cl.put(f"/api/films/{self.f}", json={"primary_genre": "thriller"})
        self.assertEqual(self.row(self.f)[0], "Thriller")
        self.assertNotIn("thriller", db.custom_primary_genres(self.c))
        self.cl.put(f"/api/films/{self.cp}", json={"primary_genre": "Neo Noir"}); self.cl.put(f"/api/films/{self.f}", json={"primary_genre": "neo noir"})
        self.assertEqual(self.row(self.f)[0], "Neo Noir"); self.assertEqual(db.custom_primary_genres(self.c).count("Neo Noir"), 1)

    def test_classic_porn_manual_allowed_auto_restricted(self):
        self.assertTrue(db.set_porn_primary_genre(self.c, self.cp)); self.assertEqual(self.row(self.cp)[0], "Hardcore")
        self.c.execute("UPDATE films SET primary_genre=NULL WHERE id=?", (self.cp,))
        self.assertTrue(db.set_primary_genre(self.c, self.cp, "Comedy")); self.assertEqual(self.row(self.cp)[0], "Hardcore")
        self.c.commit()
        self.cl.put(f"/api/films/{self.cp}", json={"primary_genre": "Porn Parody"})
        self.assertEqual(self.row(self.cp)[0], "Porn Parody")
        self.assertFalse(db.set_porn_primary_genre(self.c, self.cp)); self.assertEqual(self.row(self.cp)[0], "Porn Parody")

    def test_create_film_with_manual_genre_is_locked(self):
        r = self.cl.post("/api/films", json={"title": "New" + str(time.time()), "primary_genre": "Mondo"})
        fid = r.get_json().get("id")
        if fid: pg, ed = self.row(fid); self.assertEqual(pg, "Mondo"); self.assertIn("primary_genre", json.loads(ed))
        self.assertIn("Mondo", db.custom_primary_genres(self.c))

if __name__ == "__main__": unittest.main()
