"""Speed fixes: db.connect() runs schema/migrations once per process (later connections still get every SQL function),
and gallery thumbnail requests reuse the folder found by the listing (Review by hand still checked each time)."""
import os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, gallery, app as A

class Speed(unittest.TestCase):
    def test_connect_fast_path_keeps_functions(self):
        db.connect().close(); self.assertIn(os.path.abspath(db.DB_PATH), db._READY)
        with mock.patch.object(sqlite3_mod(), "connect", wraps=sqlite3_mod().connect):
            c = db.connect()
        for q in ("SELECT tag_norm('X')", "SELECT 'a' < 'B' COLLATE FOLD", "SELECT fold_in('a','a')", "SELECT sub_lang_in('', '')",
                  "SELECT person_in('a','a')", "SELECT COUNT(*) FROM films WHERE COALESCE(rename_pending,0)=0"):
            c.execute(q).fetchone()
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_thumbs_reuse_listing(self):
        root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [root])
        v = os.path.join(root, "S", "S.mp4"); os.makedirs(os.path.dirname(v)); open(v, "wb").close()
        g = gallery.full_dir(v); os.makedirs(os.path.join(g, "Set")); open(os.path.join(g, "Set", "a.jpg"), "wb").write(b"\xff\xd8x")
        fid = c.execute("INSERT INTO films(title,kind,added_at) VALUES('S','scene',?)", (time.time(),)).lastrowid
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, v)); c.commit(); cl = A.app.test_client()
        self.assertEqual(cl.get(f"/api/films/{fid}/gallery").get_json()["count"], 1)
        with mock.patch.object(A, "scene_gallery", side_effect=AssertionError("rescanned")):
            for _ in range(3):
                r = cl.get(f"/api/films/{fid}/gallery/image", query_string={"name": "Set/a.jpg"}); self.assertEqual(r.status_code, 200); r.close()
            self.assertEqual(cl.get(f"/api/films/{fid}/gallery/image", query_string={"name": "../S.mp4"}).status_code, 404)
        c.execute("UPDATE films SET notes='Review by hand' WHERE id=?", (fid,)); c.commit()
        self.assertEqual(cl.get(f"/api/films/{fid}/gallery/thumb", query_string={"name": "Set/a.jpg"}).status_code, 403)
        c.execute("UPDATE films SET notes=NULL WHERE id=?", (fid,)); c.commit()
        import shutil; shutil.rmtree(g)  # gallery deleted outside the app: cache is not trusted
        self.assertEqual(cl.get(f"/api/films/{fid}/gallery/thumb", query_string={"name": "Set/a.jpg"}).status_code, 404)

def sqlite3_mod():
    import sqlite3; return sqlite3

if __name__ == "__main__": unittest.main()
