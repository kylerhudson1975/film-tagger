"""Galleries with shoot subfolders one level down (FTV Girls style): listing order, groups, counts, path safety,
flat galleries unchanged, API listing and serving. Synthetic tiny files only, temp dirs."""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import gallery, db, app as A

def touch(p, data=b"\xff\xd8\xff\xe0tiny"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh: fh.write(data)

class Sub(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(); self.video = os.path.join(self.root, "Studio", "Scene.mp4"); touch(self.video, b"v")
        self.g = gallery.full_dir(self.video)
        for n in ("b 10.jpg", "b 2.jpg"): touch(os.path.join(self.g, "Shoot 10 - Outdoors", n))
        touch(os.path.join(self.g, "Shoot 2 - Hike", "a.png")); touch(os.path.join(self.g, "Shoot 2 - Hike", "notes.txt"))
        touch(os.path.join(self.g, "Shoot 2 - Hike", "deeper", "x.jpg"))  # two levels down: ignored
        touch(os.path.join(self.g, ".hidden", "h.jpg")); touch(os.path.join(self.g, "top.jpg")); touch(os.path.join(self.g, "readme.txt"))

    def test_order_groups_counts(self):
        names = gallery.images(self.g)
        self.assertEqual(names, ["top.jpg", "Shoot 2 - Hike/a.png", "Shoot 10 - Outdoors/b 2.jpg", "Shoot 10 - Outdoors/b 10.jpg"])
        self.assertEqual([(x["folder"], x["start"], x["count"]) for x in gallery.groups(names)],
                         [(None, 0, 1), ("Shoot 2 - Hike", 1, 1), ("Shoot 10 - Outdoors", 2, 2)])

    def test_only_subfolders(self):
        os.remove(os.path.join(self.g, "top.jpg"))
        self.assertEqual(len(gallery.images(self.g)), 3); self.assertEqual(gallery.groups(gallery.images(self.g))[0]["folder"], "Shoot 2 - Hike")

    def test_safe_name(self):
        ok = gallery.safe_name(self.g, "Shoot 10 - Outdoors/b 2.jpg"); self.assertTrue(ok and os.path.isfile(ok))
        self.assertTrue(gallery.safe_name(self.g, "Shoot 10 - Outdoors\\b 2.jpg"))
        self.assertTrue(gallery.safe_name(self.g, "top.jpg"))
        outside = os.path.join(self.root, "Studio", "evil.jpg"); touch(outside)
        for bad in ("../evil.jpg", "..\\evil.jpg", "Shoot 2 - Hike/../../evil.jpg", outside, "/etc/passwd", "C:\\x.jpg", "C:x.jpg",
                    "Shoot 2 - Hike/deeper/x.jpg", ".hidden/h.jpg", "Shoot 2 - Hike/notes.txt", "", "./top.jpg", "Shoot 2 - Hike//a.png", "nope.jpg"):
            self.assertIsNone(gallery.safe_name(self.g, bad), bad)
        if hasattr(os, "symlink"):
            try: os.symlink(os.path.join(self.root, "Studio"), os.path.join(self.g, "link")); self.assertIsNone(gallery.safe_name(self.g, "link/evil.jpg"))
            except OSError: pass

    def test_flat_unchanged(self):
        v = os.path.join(self.root, "Flat", "F.mp4"); touch(v, b"v"); fg = gallery.full_dir(v)
        for n in ("img10.jpg", "img2.jpg", "img1.jpg"): touch(os.path.join(fg, n))
        self.assertEqual(gallery.images(fg), ["img1.jpg", "img2.jpg", "img10.jpg"])
        self.assertEqual(gallery.groups(gallery.images(fg)), [dict(folder=None, start=0, count=3)])
        self.assertTrue(gallery.safe_name(fg, "img1.jpg")); self.assertIsNone(gallery.safe_name(fg, "../F.mp4"))

    def test_api(self):
        c = db.connect(); db.set_setting(c, "scene_roots", [self.root])
        fid = c.execute("INSERT INTO films(title,kind,added_at) VALUES('S','scene',?)", (time.time(),)).lastrowid
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, self.video)); c.commit()
        cl = A.app.test_client(); d = cl.get(f"/api/films/{fid}/gallery").get_json()
        self.assertEqual(d["count"], 4); self.assertEqual([g["folder"] for g in d["groups"]], [None, "Shoot 2 - Hike", "Shoot 10 - Outdoors"])
        self.assertEqual(c.execute("SELECT gallery_count FROM films WHERE id=?", (fid,)).fetchone()[0], 4)
        self.assertEqual(A.refresh_galleries(c).get(fid), 4)
        r = cl.get(f"/api/films/{fid}/gallery/image", query_string={"name": "Shoot 10 - Outdoors/b 10.jpg"}); self.assertEqual(r.status_code, 200); r.close()
        self.assertEqual(cl.get(f"/api/films/{fid}/gallery/image", query_string={"name": "../Scene.mp4"}).status_code, 404)
        self.assertEqual(cl.get(f"/api/films/{fid}/gallery/thumb", query_string={"name": "Shoot 2 - Hike/../../x.jpg"}).status_code, 404)
        c.execute("UPDATE films SET notes='Review by hand' WHERE id=?", (fid,)); c.commit()
        self.assertEqual(cl.get(f"/api/films/{fid}/gallery").status_code, 403)

if __name__ == "__main__": unittest.main()
