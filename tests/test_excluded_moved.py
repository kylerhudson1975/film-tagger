"""A removed file stays removed after it is moved to another folder (same name + size), even without a hash."""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db

class ExcludedMoved(unittest.TestCase):
    def test_moved_file_stays_removed(self):
        c = db.connect(); c.execute("DELETE FROM excluded")
        c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at) VALUES(?,?,?,?,?,?)",
                  (r"D:\\Misc 44\\Set\\001 - Haley Spades.mp4", 1234, None, "x", "scene", time.time())); c.commit()
        t = tempfile.mkdtemp(); p = os.path.join(t, "Team Skeet", "001 - Haley Spades.mp4"); os.makedirs(os.path.dirname(p))
        with open(p, "wb") as fh: fh.write(b"x" * 1234)
        self.assertTrue(db.is_excluded(c, p, 1234))
        self.assertFalse(db.is_excluded(c, p, 999))  # different size is a different file
        q = os.path.join(t, "Team Skeet", "002 - Other.mp4")
        with open(q, "wb") as fh: fh.write(b"y" * 1234)
        self.assertFalse(db.is_excluded(c, q, 1234))

if __name__ == "__main__": unittest.main()
