"""Cast character names: TMDB cast[].character and NFO <actor><role> fill films.cast_roles for the Edit
hover tip ("as Mathilda"). Hand-locked cast_roles are never overwritten. Matching uses TMDB person id
when known, else a normalised name (Jr./accents). The whole TMDB cast is considered, not only top 10.
Run: python tests/test_cast_roles.py"""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(TMP, "test.db"); sys.path.insert(0, ROOT)
import db, app as A

MOVIE = {
    "id": 481, "title": "The Ninth Gate", "release_date": "1999-08-25", "runtime": 133,
    "overview": "A rare book dealer…", "poster_path": "/ninth.jpg", "genres": [{"id": 53, "name": "Thriller"}],
    "credits": {
        "cast": [
            {"id": 85, "name": "Johnny Depp", "character": "Dean Corso", "order": 0},
            {"id": 1, "name": "Frank Langella", "character": "Boris Balkan", "order": 1},
            {"id": 2, "name": "Lena Olin", "character": "Liana Telfer", "order": 2},
            {"id": 3, "name": "Extra Extra", "character": "", "order": 3},
        ],
        "crew": [{"job": "Director", "name": "Roman Polanski"}],
    },
    "release_dates": {"results": []},
    "production_countries": [], "production_companies": [],
}

BRONX = {
    "id": 1607, "title": "A Bronx Tale", "release_date": "1993-09-29", "runtime": 121,
    "overview": "…", "poster_path": "/bronx.jpg", "genres": [{"id": 18, "name": "Drama"}],
    "credits": {
        "cast": [
            {"id": 380, "name": "Robert De Niro", "character": "Lorenzo Anello", "order": 0},
            {"id": 9046, "name": "Chazz Palminteri", "character": "Sonny LoSpecchio", "order": 1},
            {"id": 17922, "name": "Alfred Sauchelli, Jr.", "character": "Bobby Bars", "order": 7},
            {"id": 4517, "name": "Joe Pesci", "character": "Carmine", "order": 9},
            {"id": 17924, "name": "Robert D'Andrea", "character": "Tony Toupee", "order": 10},
        ],
        "crew": [{"job": "Director", "name": "Robert De Niro"}],
    },
    "release_dates": {"results": []},
    "production_countries": [], "production_companies": [],
}

def fake_tmdb(path, key, **p):
    if path.startswith("search/"): return {"results": [dict(MOVIE)]}
    if path.startswith("movie/1607"): return BRONX
    return MOVIE

A.tmdb = fake_tmdb


class CastRoles(unittest.TestCase):
    def setUp(self):
        c = db.connect(); db.set_setting(c, "tmdb_key", "test")
        self.fid = c.execute(
            "INSERT INTO films(title, year, added_at, updated_at) VALUES('The Ninth Gate', 1999, ?, ?)",
            (time.time(), time.time())).lastrowid
        c.commit(); c.close(); self.cl = A.app.test_client()

    def test_split_people_keeps_jr(self):
        names = A.split_people("Robert De Niro, Alfred Sauchelli, Jr., Joe Pesci")
        self.assertEqual(names, ["Robert De Niro", "Alfred Sauchelli, Jr.", "Joe Pesci"])
        self.assertEqual(A.split_people("James A. Watson, Jr, Bara Byrnes"),
                         ["James A. Watson, Jr", "Bara Byrnes"])

    def test_people_name_key(self):
        self.assertEqual(A.people_name_key("Alfred Sauchelli, Jr."), A.people_name_key("Alfred Sauchelli Jr"))
        self.assertEqual(A.people_name_key("Sergi López"), A.people_name_key("Sergi Lopez"))

    def test_from_credits_helper_full_cast(self):
        roles = A.cast_roles_from_credits(MOVIE["credits"])
        self.assertEqual(roles["Johnny Depp"], "Dean Corso")
        self.assertEqual(roles["Frank Langella"], "Boris Balkan")
        self.assertNotIn("Extra Extra", roles)  # empty character skipped
        # beyond top-10 style entry is kept when present
        roles2 = A.cast_roles_from_credits(BRONX["credits"])
        self.assertEqual(roles2["Robert D'Andrea"], "Tony Toupee")
        self.assertEqual(roles2["Alfred Sauchelli, Jr."], "Bobby Bars")

    def test_cast_roles_for_names_normalised_and_id(self):
        # Displayed cast uses the TMDB spelling with comma-Jr.
        names = ["Robert De Niro", "Alfred Sauchelli, Jr.", "Joe Pesci", "Robert D'Andrea"]
        got = A.cast_roles_for_names(BRONX["credits"], names)
        self.assertEqual(got["Alfred Sauchelli, Jr."], "Bobby Bars")
        self.assertEqual(got["Robert D'Andrea"], "Tony Toupee")
        # TMDB person id wins when the displayed name differs
        names2 = ["Bobby Bars Actor"]  # not a real credit name
        got2 = A.cast_roles_for_names(BRONX["credits"], names2, tmdb_ids_by_name={"Bobby Bars Actor": 17922})
        self.assertEqual(got2["Bobby Bars Actor"], "Bobby Bars")

    def test_merge_rewrites_onto_cast_spelling(self):
        cast_names = ["Alfred Sauchelli, Jr.", "Joe Pesci"]
        out = A.merge_cast_roles({}, {"Alfred Sauchelli, Jr.": "Bobby Bars", "Stranger": "X"},
                                 only_names=cast_names)
        self.assertEqual(out["Alfred Sauchelli, Jr."], "Bobby Bars")
        self.assertNotIn("Stranger", out)

    def test_remap_cast_roles(self):
        roles = {"Alfred Sauchelli Jr": "Bobby Bars", "Joe Pesci": "Carmine"}
        out = A.remap_cast_roles(roles, ["Alfred Sauchelli, Jr.", "Joe Pesci"])
        self.assertEqual(out["Alfred Sauchelli, Jr."], "Bobby Bars")
        self.assertEqual(out["Joe Pesci"], "Carmine")

    def test_incomplete(self):
        self.assertTrue(A.cast_roles_incomplete(
            "Robert De Niro, Alfred Sauchelli, Jr.", {"Robert De Niro": "Lorenzo"}))
        self.assertFalse(A.cast_roles_incomplete(
            "Robert De Niro, Alfred Sauchelli, Jr.",
            {"Robert De Niro": "Lorenzo", "Alfred Sauchelli Jr": "Bobby Bars"}))

    def test_apply_movie_fills_roles(self):
        c = db.connect(); A.apply_movie(c, self.fid, MOVIE); c.commit(); c.close()
        f = self.cl.get(f"/api/films/{self.fid}").get_json()
        self.assertEqual(f["cast_"], "Johnny Depp, Frank Langella, Lena Olin, Extra Extra")
        self.assertEqual(f["cast_roles"]["Johnny Depp"], "Dean Corso")
        self.assertEqual(f["cast_roles"]["Lena Olin"], "Liana Telfer")
        self.assertNotIn("Extra Extra", f["cast_roles"])

    def test_apply_movie_jr_and_beyond_top10(self):
        c = db.connect()
        fid = c.execute(
            "INSERT INTO films(title, year, cast_, tmdb_id, added_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("A Bronx Tale", 1993,
             "Robert De Niro, Alfred Sauchelli, Jr., Joe Pesci, Robert D'Andrea",
             1607, time.time(), time.time())).lastrowid
        # Link Alfred to his TMDB person id under a slightly different spelling
        c.execute("INSERT INTO people(name, name_norm, tmdb_person_id) VALUES(?,?,?)",
                  ("Alfred Sauchelli Jr", db.fold("Alfred Sauchelli Jr"), 17922))
        c.commit()
        A.apply_movie(c, fid, BRONX); c.commit(); c.close()
        f = self.cl.get(f"/api/films/{fid}").get_json()
        # apply_movie rewrites cast_ to TMDB top 10 — that's existing behaviour; roles must cover Jr. + #11
        self.assertEqual(f["cast_roles"].get("Alfred Sauchelli, Jr."), "Bobby Bars")
        # After apply, cast_ is top 10 from BRONX credits (only 5 in our fixture)
        self.assertIn("Robert D'Andrea", f["cast_"] )
        self.assertEqual(f["cast_roles"].get("Robert D'Andrea"), "Tony Toupee")

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

    def test_requeue_incomplete(self):
        c = db.connect()
        fid = c.execute(
            "INSERT INTO films(title, year, cast_, cast_roles, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,1,?,?)",
            ("A Bronx Tale", 1993, "Robert De Niro, Alfred Sauchelli, Jr., Joe Pesci",
             json.dumps({"Robert De Niro": "Lorenzo Anello", "Joe Pesci": "Carmine"}),
             time.time(), time.time())).lastrowid
        c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (fid, "/tmp/bronx.mkv", 10))
        c.commit()
        n = A.requeue_incomplete_cast_roles(c); c.commit()
        row = c.execute("SELECT cast_roles_checked FROM films WHERE id=?", (fid,)).fetchone()
        c.close()
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
