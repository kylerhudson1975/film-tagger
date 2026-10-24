"""Scan relinks files renamed/moved outside the app to their existing item (same size + fingerprint, or a single safe
candidate for rows without a stored fingerprint) instead of adding a duplicate. Temp dirs, synthetic files."""
import json, os, shutil, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db

def write(p, seed, n=300000):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh: fh.write(bytes((seed * 7 + i) % 251 for i in range(n)))

class Relink(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(); self.c.execute("DELETE FROM films"); self.c.execute("DELETE FROM excluded"); self.c.commit()
        self.mov = tempfile.mkdtemp(); self.sc = tempfile.mkdtemp(); db.set_setting(self.c, "scene_roots", [self.sc])
    def scan(self): return db.scan(self.c, [self.mov, self.sc])
    def films(self): return self.c.execute("SELECT COUNT(*) FROM films").fetchone()[0]
    def path(self, fid): return self.c.execute("SELECT path, missing FROM files WHERE film_id=?", (fid,)).fetchone()

    def test_scene_renamed_keeps_metadata(self):
        a = os.path.join(self.sc, "Studio", "old name.mp4"); write(a, 1); self.scan()
        fid = self.c.execute("SELECT id FROM films").fetchone()[0]
        self.c.execute("UPDATE films SET title='Kept', tpdb_id='t1', edited_fields='[\"title\"]' WHERE id=?", (fid,)); db.add_tag(self.c, fid, "Blonde", "Content"); self.c.commit()
        b = os.path.join(self.sc, "Studio", "Sub", "new name.mp4"); os.makedirs(os.path.dirname(b)); os.rename(a, b)
        r = self.scan(); self.assertEqual((r["relinked"], r["added"]), (1, 0)); self.assertEqual(self.films(), 1)
        self.assertEqual(tuple(self.path(fid)), (b, 0))
        f = self.c.execute("SELECT title, tpdb_id, edited_fields, missing FROM films WHERE id=?", (fid,)).fetchone()
        self.assertEqual(tuple(f), ("Kept", "t1", '["title"]', 0))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM film_tags WHERE film_id=?", (fid,)).fetchone()[0], 1)
        self.assertTrue(os.path.exists(os.path.join(self.sc, "_filmtagger_relinks.csv")))

    def test_movie_without_stored_fingerprint(self):  # rows from before fingerprints were stored
        a = os.path.join(self.mov, "Film (1999).mkv"); write(a, 2); self.scan(); fid = self.c.execute("SELECT id FROM films").fetchone()[0]
        self.c.execute("UPDATE files SET oshash=NULL"); self.c.commit()
        b = os.path.join(self.mov, "Film.1999.mkv"); os.rename(a, b)
        self.assertEqual(self.scan()["relinked"], 1); self.assertEqual(self.path(fid)[0], b)

    def test_two_candidates_adds_new(self):
        a1, a2 = os.path.join(self.mov, "A.mkv"), os.path.join(self.mov, "B.mkv"); write(a1, 3); write(a2, 3); self.scan()
        self.c.execute("UPDATE files SET oshash=NULL"); self.c.commit()  # identical size, no fingerprints: ambiguous
        os.remove(a1); os.rename(a2, os.path.join(self.mov, "C.mkv"))
        r = self.scan(); self.assertEqual((r["relinked"], r["added"]), (0, 1)); self.assertEqual(self.films(), 3)

    def test_two_with_same_fingerprint_adds_new(self):
        a1, a2 = os.path.join(self.mov, "A.mkv"), os.path.join(self.mov, "B.mkv"); write(a1, 3); write(a2, 3); self.scan()
        os.remove(a1); os.rename(a2, os.path.join(self.mov, "C.mkv"))
        self.assertEqual(self.scan()["relinked"], 0)

    def test_size_differs_adds_new(self):
        a = os.path.join(self.mov, "A.mkv"); write(a, 4); self.scan(); os.remove(a)
        write(os.path.join(self.mov, "A2.mkv"), 4, n=300001)
        r = self.scan(); self.assertEqual((r["relinked"], r["added"]), (0, 1))

    def test_removed_stays_removed(self):
        a = os.path.join(self.mov, "A.mkv"); write(a, 5); self.scan(); fid = self.c.execute("SELECT id FROM films").fetchone()[0]
        db.exclude_film(self.c, fid); self.c.commit(); os.rename(a, os.path.join(self.mov, "Z.mkv"))
        r = self.scan(); self.assertEqual((r["relinked"], r["added"]), (0, 0)); self.assertEqual(self.films(), 0)

    def test_existing_file_untouched_and_other_kind(self):
        a = os.path.join(self.mov, "A.mkv"); write(a, 6); self.scan(); fid = self.c.execute("SELECT id FROM films").fetchone()[0]
        r = self.scan(); self.assertEqual((r["relinked"], r["added"]), (0, 0)); self.assertEqual(tuple(self.path(fid)), (a, 0))
        self.c.execute("UPDATE files SET oshash=NULL"); self.c.commit()  # no fingerprint: a movie file never relinks to a scene
        os.makedirs(os.path.join(self.sc, "S")); shutil.move(a, os.path.join(self.sc, "S", "A.mkv"))
        r = self.scan(); self.assertEqual((r["relinked"], r["added"]), (0, 1))

    def test_unplugged_library_not_stolen(self):
        a = os.path.join(self.mov, "A.mkv"); write(a, 7); self.scan(); fid = self.c.execute("SELECT id FROM films").fetchone()[0]
        copy = os.path.join(self.sc, "S", "A copy.mkv"); os.makedirs(os.path.dirname(copy)); shutil.copy(a, copy)
        shutil.rmtree(self.mov)  # movies drive unplugged
        r = db.scan(self.c, []); self.assertEqual(r["relinked"], 0); self.assertEqual(self.path(fid)[0], a)

if __name__ == "__main__": unittest.main()
