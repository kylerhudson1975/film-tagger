"""Settings > Removed items: studio A-Z (none last), first performer A-Z, newest first; studio/performers from DB or path."""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class ExcludedSort(unittest.TestCase):
    def test_order_and_fields(self):
        root = os.path.join(tempfile.mkdtemp(), "Porn Scenes"); c = db.connect(); db.set_setting(c, "scene_roots", [root]); c.execute("DELETE FROM excluded")
        def add(path, t, studio=None, cast=None):
            c.execute("INSERT INTO excluded(path,size,title,kind,removed_at,studio,cast_) VALUES(?,?,?,?,?,?,?)", (path, 1, "T", "scene", t, studio, cast))
        add(os.path.join(root, "Perv Mom", "Perv Mom - 2023-01-01 - Zoe Z - X.mp4"), 1)       # from path
        add(os.path.join(root, "Perv Mom", "Perv Mom - 2023-01-02 - Ada A - Y.mp4"), 2)
        add("/elsewhere/movie.mkv", 9); add(os.path.join(root, "_No Studio", "Ryan Conner Skylar Snow.mp4"), 8)                                                      # no studio -> last
        add("/x/a.mp4", 3, "Angels Love", "Bea B, Cy C"); add("/x/b.mp4", 5, "Angels Love", "Bea B")  # stored; newest first on tie
        c.commit()
        items = A.app.test_client().get("/api/excluded").get_json()["items"]
        self.assertEqual([(x["studio"], x["performers"][:1], x["removed_at"]) for x in items],
                         [("Angels Love", ["Bea B"], 5), ("Angels Love", ["Bea B"], 3), ("Perv Mom", ["Ada A"], 2), ("Perv Mom", ["Zoe Z"], 1), ("", ["Ryan Conner Skylar Snow"], 8), ("", [], 9)])

if __name__ == "__main__": unittest.main()
