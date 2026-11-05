"""GET /api/films/<id> must not probe gallery folders on the request thread (slow/offline drives hang the UI)."""
import os, sys, tempfile, time, unittest
from unittest import mock
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import db, app as A

class GetFilmNoFs(unittest.TestCase):
    def test_get_film_does_not_call_scene_gallery_inline(self):
        c = db.connect()
        fid = c.execute("INSERT INTO films(title,kind,added_at) VALUES('X','scene',?)", (time.time(),)).lastrowid
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, r"D:\missing\scene.mp4")); c.commit()
        called = []
        def boom(*a, **k):
            called.append(1); raise AssertionError("scene_gallery ran on request thread")
        with mock.patch.object(A, "scene_gallery", side_effect=boom), mock.patch.object(A, "kick_gallery") as kg:
            r = A.app.test_client().get(f"/api/films/{fid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(called, [])
        kg.assert_called_once_with(fid)

if __name__ == "__main__":
    unittest.main()
