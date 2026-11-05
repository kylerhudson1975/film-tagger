"""Artwork backup copies in the video folders (artexport.py): poster.jpg naming, never overwriting a foreign file,
idempotence, replacing only Film Tagger's own copy (old one backed up), person photo to one deterministic folder.
Synthetic bytes only, temp DB + temp folders. Run: python -m pytest tests/test_art_export.py"""
import hashlib, io, os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

def jpg(tag): return b"\xff\xd8\xff\xe0" + tag.encode() * 50
def h(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()

class ArtExport(unittest.TestCase):
    def setUp(self):
        self.c = db.connect(); self.cl = A.app.test_client(); self.root = tempfile.mkdtemp()
        self.d1 = os.path.join(self.root, "Film (1983)"); self.d2 = os.path.join(self.root, "Film CD2"); os.makedirs(self.d1); os.makedirs(self.d2)
        self.fid = self.c.execute("INSERT INTO films(title,kind,cast_,added_at) VALUES('Film','movie',?,?)", (self.id().split('.')[-1].replace('_', ' ') + ' Roe, Ann Two', time.time())).lastrowid
        for i, d in enumerate((self.d1, self.d2, self.d1)):
            self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (self.fid, os.path.join(d, f"v{i}-{self.fid}.mkv")))
            open(os.path.join(d, f"v{i}-{self.fid}.mkv"), "wb").write(b"video")
        self.c.commit()
        self.p = mock.patch.object(A, "kick_focus", lambda: None); self.p.start()
    def tearDown(self): self.p.stop()
    def upload(self, data):
        r = self.cl.post(f"/api/films/{self.fid}/poster", data={"file": (io.BytesIO(data), "p.jpg", "image/jpeg")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200); return r.get_json()

    def test_poster_copied_to_each_folder_and_idempotent(self):
        j = self.upload(jpg("a"))
        self.assertEqual(sorted(x["folder"] for x in j["art_export"]), sorted([self.d1, self.d2]))
        for d in (self.d1, self.d2): self.assertEqual(open(os.path.join(d, "poster.jpg"), "rb").read(), jpg("a"))
        r = self.cl.post(f"/api/films/{self.fid}/artwork/export").get_json()["art_export"]
        self.assertTrue(all(x["status"] == "same" for x in r))
        self.assertEqual(open(os.path.join(self.d1, "v0-%d.mkv" % self.fid), "rb").read(), b"video")

    def test_foreign_poster_never_overwritten(self):
        open(os.path.join(self.d1, "Poster.jpg"), "wb").write(jpg("user")); before = h(os.path.join(self.d1, "Poster.jpg"))
        j = self.upload(jpg("b"))
        self.assertEqual(h(os.path.join(self.d1, "Poster.jpg")), before)
        self.assertEqual(open(os.path.join(self.d1, "poster-filmtagger.jpg"), "rb").read(), jpg("b"))
        e = next(x for x in j["art_export"] if x["folder"] == self.d1); self.assertEqual(e["file"], "poster-filmtagger.jpg")

    def test_existing_identical_poster_is_reused(self):
        open(os.path.join(self.d1, "Poster.jpg"), "wb").write(jpg("same"))
        j = self.upload(jpg("same"))
        e = next(x for x in j["art_export"] if x["folder"] == self.d1)
        self.assertEqual((e["file"], e["status"]), ("Poster.jpg", "same"))
        self.assertEqual(sorted(n for n in os.listdir(self.d1) if not n.endswith(".mkv")), ["Poster.jpg"])

    def test_own_copy_replaced_with_backup_and_removal_keeps_folder_art(self):
        self.upload(jpg("one")); time.sleep(1.1); self.upload(jpg("two"))
        self.assertEqual(open(os.path.join(self.d1, "poster.jpg"), "rb").read(), jpg("two"))
        bk = os.path.join(self.d1, "Film Tagger Artwork", "backup")
        self.assertEqual([open(os.path.join(bk, n), "rb").read() for n in os.listdir(bk)], [jpg("one")])
        self.cl.delete(f"/api/films/{self.fid}/poster")
        self.assertTrue(os.path.isfile(os.path.join(self.d1, "poster.jpg")))
        # a user change to our poster.jpg makes it foreign again: never overwritten
        open(os.path.join(self.d1, "poster.jpg"), "wb").write(jpg("mine")); self.upload(jpg("three"))
        self.assertEqual(open(os.path.join(self.d1, "poster.jpg"), "rb").read(), jpg("mine"))
        self.assertEqual(open(os.path.join(self.d1, "poster-filmtagger.jpg"), "rb").read(), jpg("three"))

    def test_person_photo_one_folder(self):
        r = self.cl.post("/api/people/new", json={"name": self.id().split('.')[-1].replace('_', ' ') + " Roe"}).get_json(); pid = r["id"]
        j = self.cl.post(f"/api/person/{pid}/photo/upload", data={"photo": (io.BytesIO(jpg("p")), "x.jpg")}, content_type="multipart/form-data").get_json()
        e = j["art_export"]; self.assertEqual(e["status"], "written"); self.assertEqual(e["folder"], self.d1)
        self.assertEqual(e["file"], os.path.join(self.d1, "Film Tagger Artwork", "People", "test person photo one folder Roe.jpg"))
        self.assertEqual(open(e["file"], "rb").read(), jpg("p"))
        self.assertFalse(os.path.exists(os.path.join(self.d2, "Film Tagger Artwork")))
        self.assertEqual(self.cl.post(f"/api/person/{pid}/artwork/export").get_json()["art_export"]["status"], "same")

    def test_person_without_folder(self):
        pid = self.cl.post("/api/people/new", json={"name": "Nobody Elsewhere"}).get_json()["id"]
        j = self.cl.post(f"/api/person/{pid}/photo/upload", data={"photo": (io.BytesIO(jpg("n")), "x.jpg")}, content_type="multipart/form-data").get_json()
        self.assertEqual(j["art_export"]["status"], "no video folder")

if __name__ == "__main__": unittest.main()
