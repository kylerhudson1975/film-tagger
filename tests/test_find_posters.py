"""Find Posters: lists every TMDB poster for a movie's existing TMDB id (sorted en/no-language first, then votes),
sets a picked one as a protected custom poster with the previous poster backed up, and restores it.
Temp DB, mocked TMDB API and image downloads (nothing leaves the machine). Run: python -m pytest tests/test_find_posters.py"""
import io, json, os, sys, tempfile, time, unittest, urllib.error
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

JPG = b"\xff\xd8\xff\xe0" + b"x" * 100
IMAGES = {"id": 9453, "posters": [
    {"file_path": "/de1.jpg", "iso_639_1": "de", "width": 1000, "height": 1500, "vote_average": 9.0, "vote_count": 50},
    {"file_path": "/en_low.jpg", "iso_639_1": "en", "width": 500, "height": 750, "vote_average": 3.0, "vote_count": 2},
    {"file_path": "/none.jpg", "iso_639_1": None, "width": 2000, "height": 3000, "vote_average": 5.5, "vote_count": 4},
    {"file_path": "/en_hi.jpg", "iso_639_1": "en", "width": 1400, "height": 2100, "vote_average": 5.5, "vote_count": 9},
    {"file_path": "../evil.jpg", "iso_639_1": "en", "width": 1, "height": 1}]}

class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

class FindPosters(unittest.TestCase):
    def setUp(self):
        self._tmdb, self._open = A.tmdb, A.urllib.request.urlopen
        self.calls = []; self.urls = []
        def fake_tmdb(path, key, **p): self.calls.append((path, key, p)); return json.loads(json.dumps(IMAGES))
        def fake_open(req, timeout=None): self.urls.append(req.full_url); return Resp(JPG)
        A.tmdb = fake_tmdb; A.urllib.request.urlopen = fake_open
        c = db.connect(); db.set_setting(c, "tmdb_key", "secret-key")
        now = time.time()
        self.fid = c.execute("INSERT INTO films(title,kind,tmdb_id,poster,added_at) VALUES('Caligula','movie',9453,'https://image.tmdb.org/t/p/w342/orig.jpg',?)", (now,)).lastrowid
        self.nomatch = c.execute("INSERT INTO films(title,kind,added_at) VALUES('Unknown','movie',?)", (now,)).lastrowid
        self.scene = c.execute("INSERT INTO films(title,kind,tmdb_id,added_at) VALUES('A scene','scene',1,?)", (now,)).lastrowid
        c.commit(); self.cl = A.app.test_client()
    def tearDown(self): A.tmdb, A.urllib.request.urlopen = self._tmdb, self._open

    def test_lists_sorted_posters_with_one_request(self):
        j = self.cl.get(f"/api/films/{self.fid}/findposters").get_json()
        self.assertEqual(len(self.calls), 1)
        path, key, p = self.calls[0]
        self.assertEqual(path, "movie/9453/images"); self.assertEqual(key, "secret-key")
        self.assertTrue(p["include_image_language"].startswith("en,null,"))
        self.assertEqual([x["file_path"] for x in j["posters"]], ["/en_hi.jpg", "/none.jpg", "/en_low.jpg", "/de1.jpg"])
        self.assertEqual(j["posters"][0]["thumb"], "https://image.tmdb.org/t/p/w342/en_hi.jpg")
        self.assertEqual((j["posters"][1]["lang"], j["posters"][1]["width"], j["posters"][1]["height"]), (None, 2000, 3000))
        self.assertNotIn("secret-key", json.dumps(j))

    def test_not_matched(self):
        j = self.cl.get(f"/api/films/{self.nomatch}/findposters").get_json()
        self.assertFalse(j["matched"]); self.assertEqual(j["error"], "Not matched on TMDB"); self.assertEqual(j["posters"], [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.cl.post(f"/api/films/{self.nomatch}/findposters/set", json={"file_path": "/a.jpg"}).status_code, 400)

    def test_never_for_scenes(self):
        self.assertEqual(self.cl.get(f"/api/films/{self.scene}/findposters").status_code, 400)
        self.assertEqual(self.cl.post(f"/api/films/{self.scene}/findposters/set", json={"file_path": "/a.jpg"}).status_code, 400)
        self.assertEqual(self.calls, [])

    def test_tmdb_error_hides_key(self):
        def boom(path, key, **p): raise urllib.error.URLError("https://api.themoviedb.org/3/x?api_key=" + key)
        A.tmdb = boom
        r = self.cl.get(f"/api/films/{self.fid}/findposters")
        self.assertEqual(r.status_code, 502); self.assertNotIn("secret-key", r.get_data(as_text=True))

    def test_rejects_bad_path(self):
        r = self.cl.post(f"/api/films/{self.fid}/findposters/set", json={"file_path": "../../x.jpg"})
        self.assertEqual(r.status_code, 400); self.assertEqual(self.urls, [])

    def test_set_backup_restore_and_protected(self):
        c = db.connect()
        old = A._save_poster(c, self.fid, b"\xff\xd8\xffOLD", "jpg")
        c.execute("UPDATE films SET custom_poster=? WHERE id=?", (old, self.fid)); c.commit()
        j = self.cl.post(f"/api/films/{self.fid}/findposters/set", json={"file_path": "/en_hi.jpg"}).get_json()
        self.assertEqual(self.urls, ["https://image.tmdb.org/t/p/original/en_hi.jpg"])  # one polite download
        new = j["custom_poster"]; self.assertTrue(new.startswith("/posters/film-")); self.assertTrue(j["can_restore"])
        self.assertIn("custom_poster", json.loads(db.connect().execute("SELECT edited_fields FROM films WHERE id=?", (self.fid,)).fetchone()[0]))
        with open(os.path.join(A.POSTERS_DIR, os.path.basename(new)), "rb") as fh: self.assertEqual(fh.read(), JPG)
        bak = os.path.join(A.POSTER_BACKUP_DIR, os.path.basename(old))
        self.assertTrue(os.path.exists(bak)); self.assertFalse(os.path.exists(os.path.join(A.POSTERS_DIR, os.path.basename(old))))
        # a later TMDB fetch never replaces the picked poster
        A.tmdb = lambda path, key, **p: {"id": 9453, "title": "Caligula", "poster_path": "/other.jpg", "genres": []}
        j2 = self.cl.post(f"/api/films/{self.fid}/tmdb", json={"tmdb_id": "9453"}).get_json()
        self.assertEqual(j2["custom_poster"], new)
        # restore puts the old file back; the picked one is kept in backup
        j3 = self.cl.post(f"/api/films/{self.fid}/findposters/restore", json={}).get_json()
        self.assertEqual(j3["custom_poster"], old); self.assertTrue(os.path.exists(os.path.join(A.POSTERS_DIR, os.path.basename(old))))
        self.assertTrue(os.path.exists(os.path.join(A.POSTER_BACKUP_DIR, os.path.basename(new))))
        self.assertEqual(self.cl.post(f"/api/films/{self.fid}/findposters/restore", json={}).status_code, 400)

    def test_restore_to_tmdb_poster_when_no_custom(self):
        self.cl.post(f"/api/films/{self.fid}/findposters/set", json={"file_path": "/none.jpg"})
        j = self.cl.post(f"/api/films/{self.fid}/findposters/restore", json={}).get_json()
        self.assertIsNone(j["custom_poster"]); self.assertEqual(j["poster"], "https://image.tmdb.org/t/p/w342/orig.jpg")

    def test_falls_back_to_w780_and_shrinks_huge(self):
        seen = []
        def fake_open(req, timeout=None):
            seen.append(req.full_url)
            if "/original/" in req.full_url: raise urllib.error.HTTPError(req.full_url, 500, "x", {}, None)
            return Resp(b"\xff\xd8\xff" + b"y" * (A.POSTER_MAX + 10))
        A.urllib.request.urlopen = fake_open
        fit = []; orig_fit = A.fit_poster; A.fit_poster = lambda d: (fit.append(len(d)), (JPG, "jpg"))[1]
        try: j = self.cl.post(f"/api/films/{self.fid}/findposters/set", json={"file_path": "/de1.jpg"}).get_json()
        finally: A.fit_poster = orig_fit
        self.assertEqual([u.split("/t/p/")[1] for u in seen], ["original/de1.jpg", "w780/de1.jpg"])
        self.assertEqual(len(fit), 1); self.assertTrue(j["custom_poster"].startswith("/posters/"))

if __name__ == "__main__": unittest.main()
