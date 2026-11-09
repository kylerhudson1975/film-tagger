"""Cast character names: TMDB cast[].character and NFO <actor><role> fill films.cast_roles for the Edit
hover tip ("as Mathilda"). Hand-locked cast_roles are never overwritten. Run: python tests/test_cast_roles.py"""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(TMP, "test.db"); sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {
    "id": 481, "title": "The Ninth Gate", "release_date": "1999-08-25", "runtime": 133,
    "overview": "A rare book dealer…", "poster_path": "/ninth.jpg", "genres": [{"id": 53, "name": "Thriller"}],
    "credits": {
        "cast": [
            {"name": "Johnny Depp", "character": "Dean Corso", "order": 0},
            {"name": "Frank Langella", "character": "Boris Balkan", "order": 1},
            {"name": "Lena Olin", "character": "Liana Telfer", "order": 2},
            {"name": "Extra Extra", "character": "", "order": 3},
        ],
        "crew": [{"job": "Director", "name": "Roman Polanski"}],
    },
    "release_dates": {"results": []},
    "production_countries": [], "production_companies": [],
}

def fake_tmdb(path, key, **p):
    if path.startswith("search/"): return {"results": [dict(MOVIE)]}
    return MOVIE

A.tmdb = fake_tmdb


class CastRoles(unittest.TestCase):
    def setUp(self):
        c = db.connect(); db.set_setting(c, "tmdb_key", "test")
        self.fid = c.execute(
            "INSERT INTO films(title, year, added_at, updated_at) VALUES('The Ninth Gate', 1999, ?, ?)",
            (time.time(), time.time())).lastrowid
        c.commit(); c.close(); self.cl = A.app.test_client()

    def test_from_credits_helper(self):
        roles = A.cast_roles_from_credits(MOVIE["credits"])
        self.assertEqual(roles["Johnny Depp"], "Dean Corso")
        self.assertEqual(roles["Frank Langella"], "Boris Balkan")
        self.assertNotIn("Extra Extra", roles)  # empty character skipped

    def test_apply_movie_fills_roles(self):
        c = db.connect(); A.apply_movie(c, self.fid, MOVIE); c.commit(); c.close()
        f = self.cl.get(f"/api/films/{self.fid}").get_json()
        self.assertEqual(f["cast_"], "Johnny Depp, Frank Langella, Lena Olin, Extra Extra")
        self.assertEqual(f["cast_roles"]["Johnny Depp"], "Dean Corso")
        self.assertEqual(f["cast_roles"]["Lena Olin"], "Liana Telfer")
        self.assertNotIn("Extra Extra", f["cast_roles"])

    def test_hand_lock_not_overwritten(self):
        c = db.connect()
        c.execute("UPDATE films SET cast_roles=?, edited_fields=?, cast_='Johnny Depp' WHERE id=?",
                  (json.dumps({"Johnny Depp": "My Corso"}), json.dumps(["cast_roles"]), self.fid))
        c.commit()
        A.apply_movie(c, self.fid, MOVIE); c.commit(); c.close()
        f = self.cl.get(f"/api/films/{self.fid}").get_json()
        self.assertEqual(f["cast_roles"]["Johnny Depp"], "My Corso")

    def test_nfo_roles(self):
        folder = tempfile.mkdtemp(); vid = os.path.join(folder, "Leon (1994).mkv")
        open(vid, "wb").write(b"x" * 20)
        nfo = os.path.join(folder, "Leon (1994).nfo")
        open(nfo, "w", encoding="utf-8").write(
            '<?xml version="1.0"?>\n<movie><actor><name>Jean Reno</name><role>Léon</role></actor>'
            '<actor><name>Natalie Portman</name><role>Mathilda</role></actor></movie>\n')
        roles = A.parse_nfo_roles(nfo)
        self.assertEqual(roles, {"Jean Reno": "Léon", "Natalie Portman": "Mathilda"})
        c = db.connect()
        fid = c.execute("INSERT INTO films(title, year, cast_, added_at, updated_at) VALUES('Léon',1994,'Jean Reno, Natalie Portman',?,?)",
                        (time.time(), time.time())).lastrowid
        c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (fid, vid, 20))
        c.commit()
        got = A.nfo_roles_for_film(c, fid); c.close()
        self.assertEqual(got["Natalie Portman"], "Mathilda")

    def test_merge_add_only(self):
        cur = {"A": "One"}
        out = A.merge_cast_roles(cur, {"A": "Two", "B": "Bee"}, only_names={"A", "B"})
        self.assertEqual(out, {"A": "One", "B": "Bee"})  # existing kept

    def test_tmdb_fetch_endpoint(self):
        c = db.connect(); c.execute("UPDATE films SET tmdb_id=481 WHERE id=?", (self.fid,)); c.commit(); c.close()
        r = self.cl.post(f"/api/films/{self.fid}/tmdb", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        f = self.cl.get(f"/api/films/{self.fid}").get_json()
        self.assertEqual(f["cast_roles"].get("Johnny Depp"), "Dean Corso")


if __name__ == "__main__":
    unittest.main()
