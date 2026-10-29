"""Removing a film, then saving a different copy (e.g. 1080p for 720p) under the same path: the scan adds it.
The removed file itself (same path, same size) stays removed."""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db

class ExcludedReplaced(unittest.TestCase):
    def test_replacement_at_same_path_is_added(self):
        c = db.connect(); c.execute("DELETE FROM excluded"); c.execute("DELETE FROM files"); c.execute("DELETE FROM films")
        root = tempfile.mkdtemp(); d = os.path.join(root, "84 Charing Cross Road (1987)"); os.makedirs(d)
        p = os.path.join(d, "84 Charing Cross Road (1987).mkv")
        c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at) VALUES(?,?,?,?,?,?)",
                  (p.upper(), 1000, None, "84 Charing Cross Road", None, time.time())); c.commit()
        with open(p, "wb") as fh: fh.write(b"x" * 1000)
        self.assertTrue(db.is_excluded(c, p, 1000))  # the removed copy itself stays removed
        with open(p, "wb") as fh: fh.write(b"y" * 2500)  # replaced by a bigger copy, same name
        self.assertFalse(db.is_excluded(c, p, 2500))
        r = db.scan(c, [root])
        self.assertEqual(r["added"], 1); self.assertEqual(r["skipped_removed"], 0)
        self.assertEqual(c.execute("SELECT title, year FROM films").fetchone()[:], ("84 Charing Cross Road", 1987))

if __name__ == "__main__": unittest.main()
