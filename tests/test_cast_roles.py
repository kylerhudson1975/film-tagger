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

    def test_nfo_duplicate_crewish_upgraded(self):
        # Succubus-style .nfo: crew row first (role=Producer), later cast row with the real character
        folder = tempfile.mkdtemp(); nfo = os.path.join(folder, "movie.nfo")
        open(nfo, "w", encoding="utf-8").write(
            '<?xml version="1.0"?><movie>'
            '<actor><name>Adrian Hoven</name><role>Producer</role></actor>'
            '<actor><name>Pier A. Caminnecci</name><role>Producer</role></actor>'
            '<actor><name>Adrian Hoven</name><role>Ralf Drawes</role></actor>'
            '<actor><name>Pier A. Caminnecci</name><role>Hermann</role></actor>'
            '<actor><name>Janine Reynaud</name><role>Lorna Green</role></actor>'
            '<actor><name>Eva Brauner</name><role></role></actor>'
            '</movie>')
        roles = A.parse_nfo_roles(nfo)
        self.assertEqual(roles["Adrian Hoven"], "Ralf Drawes")
        self.assertEqual(roles["Pier A. Caminnecci"], "Hermann")
        self.assertEqual(roles["Janine Reynaud"], "Lorna Green")
        self.assertNotIn("Eva Brauner", roles)  # empty role skipped

    def test_merge_replace_crewish(self):
        cur = {"Adrian Hoven": "Producer", "Janine Reynaud": "Lorna Green", "Jesús Franco": "Writer"}
        incoming = {"Adrian Hoven": "Ralf Drawes", "Jesús Franco": "Writer", "New": "X"}
        out = A.merge_cast_roles(cur, incoming, only_names=["Adrian Hoven", "Janine Reynaud", "Jesús Franco"],
                                 replace_crewish=True)
        self.assertEqual(out["Adrian Hoven"], "Ralf Drawes")  # Producer upgraded
        self.assertEqual(out["Janine Reynaud"], "Lorna Green")  # kept
        self.assertEqual(out["Jesús Franco"], "Writer")  # crewish<-crewish stays
        self.assertNotIn("New", out)
        # without replace_crewish, Producer stays
        out2 = A.merge_cast_roles(cur, incoming, only_names=["Adrian Hoven"])
        self.assertEqual(out2["Adrian Hoven"], "Producer")

    def test_requeue_crewish(self):
        c = db.connect()
        fid = c.execute(
            "INSERT INTO films(title, year, cast_, cast_roles, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,1,?,?)",
            ("Succubus", 1968, "Adrian Hoven, Janine Reynaud",
             json.dumps({"Adrian Hoven": "Producer", "Janine Reynaud": "Lorna Green"}),
             time.time(), time.time())).lastrowid
        c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (fid, "/tmp/succubus.mkv", 10))
        c.commit()
        self.assertTrue(A.cast_roles_has_crewish(
            "Adrian Hoven, Janine Reynaud",
            {"Adrian Hoven": "Producer", "Janine Reynaud": "Lorna Green"}))
        n = A.requeue_incomplete_cast_roles(c); c.commit()
        row = c.execute("SELECT cast_roles_checked FROM films WHERE id=?", (fid,)).fetchone()
        c.close()
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(row[0])

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


    def test_empty_cast_filled_from_tmdb(self):
        """Films with a TMDB id but no cast_ get names (and characters) from credits; hand-locked cast_ stays empty."""
        c = db.connect()
        fid = c.execute(
            "INSERT INTO films(title, year, tmdb_id, cast_, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,NULL,?,?)",
            ("The Ninth Gate", 1999, 481, None, time.time(), time.time())).lastrowid
        c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (fid, "/tmp/ninth.mkv", 10))
        locked = c.execute(
            "INSERT INTO films(title, year, tmdb_id, cast_, edited_fields, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("Locked", 1999, 481, None, json.dumps(["cast_"]), None, time.time(), time.time())).lastrowid
        c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (locked, "/tmp/locked.mkv", 10))
        c.commit(); c.close()
        A.run_cast_roles_job("test", wait=0, tmdb_gap=0.01)
        for _ in range(100):
            if not A.CRJ.get("running"): break
            time.sleep(0.05)
        f = self.cl.get(f"/api/films/{fid}").get_json()
        self.assertIn("Johnny Depp", f["cast_"] or "")
        self.assertEqual(f["cast_roles"].get("Johnny Depp"), "Dean Corso")
        f2 = self.cl.get(f"/api/films/{locked}").get_json()
        self.assertFalse(f2.get("cast_"))  # hand-locked empty cast untouched

    def test_tmdb_character_from_roles(self):
        self.assertEqual(A.tmdb_character({"character": "Dean Corso"}), "Dean Corso")
        self.assertEqual(A.tmdb_character({
            "name": "Tim Curry",
            "roles": [{"character": "Pennywise"}, {"character": "Pennywise", "episode_count": 2}],
        }), "Pennywise")
        self.assertEqual(A.tmdb_character({
            "name": "Someone",
            "roles": [{"character": "A"}, {"character": "B"}],
        }), "A / B")

    def test_apply_tv_aggregate_credits_fills_hover_roles(self):
        c = db.connect()
        fid = c.execute(
            "INSERT INTO films(title, year, added_at, updated_at) VALUES(?,?,?,?)",
            ("Storm of the Century", 1999, time.time(), time.time())).lastrowid
        itid = c.execute(
            "INSERT INTO films(title, year, added_at, updated_at) VALUES(?,?,?,?)",
            ("IT", 1990, time.time(), time.time())).lastrowid
        storm = {
            "id": 5028, "name": "Storm of the Century", "first_air_date": "1999-02-14",
            "episode_run_time": [90], "overview": "A storm arrives.",
            "aggregate_credits": {
                "cast": [
                    {"id": 101, "name": "Colm Feore", "roles": [{"character": "Andre Linoge", "episode_count": 3}]},
                    {"id": 102, "name": "Tim Daly", "roles": [{"character": "Mike Anderson"}]},
                ],
                "crew": [{"name": "Craig R. Baxley", "jobs": [{"job": "Director"}]}],
            },
        }
        it = {
            "id": 19614, "name": "It", "first_air_date": "1990-11-18",
            "aggregate_credits": {
                "cast": [
                    {"id": 201, "name": "Tim Curry", "roles": [{"character": "Pennywise"}]},
                    {"id": 202, "name": "Richard Thomas", "roles": [{"character": "Bill Denbrough"}]},
                ],
            },
            "created_by": [{"name": "Tommy Lee Wallace"}],
        }
        A.apply_movie(c, fid, storm, tmdb_kind="tv")
        A.apply_movie(c, itid, it, tmdb_kind="tv")
        c.commit(); c.close()
        s = self.cl.get(f"/api/films/{fid}").get_json()
        self.assertEqual(s["tmdb_kind"], "tv")
        self.assertEqual(s["tmdb_id"], 5028)
        self.assertIn("Colm Feore", s["cast_"] or "")
        self.assertEqual(s["cast_roles"].get("Colm Feore"), "Andre Linoge")
        self.assertEqual(s["director"], "Craig R. Baxley")
        i = self.cl.get(f"/api/films/{itid}").get_json()
        self.assertEqual(i["cast_roles"].get("Tim Curry"), "Pennywise")
        self.assertIn("Tim Curry", i["cast_"] or "")
        self.assertEqual(i["director"], "Tommy Lee Wallace")  # created_by fallback when no Director job

    def test_tv_cast_roles_hand_lock_and_requeue(self):
        c = db.connect()
        tv = c.execute(
            "INSERT INTO films(title, year, tmdb_id, tmdb_kind, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,1,?,?)",
            ("IT", 1990, 19614, "tv", time.time(), time.time())).lastrowid
        locked = c.execute(
            "INSERT INTO films(title, year, tmdb_id, tmdb_kind, cast_roles, edited_fields, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
            ("Salem's Lot", 1979, 16118, "tv", json.dumps({"David Soul": "Keep Me"}),
             json.dumps(["cast_roles"]), time.time(), time.time())).lastrowid
        movie = c.execute(
            "INSERT INTO films(title, year, tmdb_id, tmdb_kind, cast_roles_checked, added_at, updated_at) VALUES(?,?,?,?,1,?,?)",
            ("The Ninth Gate", 1999, 481, "movie", time.time(), time.time())).lastrowid
        for fid, path in ((tv, "/tmp/it.mkv"), (locked, "/tmp/salem.mkv"), (movie, "/tmp/ninth2.mkv")):
            c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (fid, path, 10))
        n = A.requeue_tv_cast_roles(c); c.commit()
        flags = {r[0]: r[1] for r in c.execute(
            "SELECT id, cast_roles_checked FROM films WHERE id IN (?,?,?)", (tv, locked, movie))}
        c.close()
        self.assertGreaterEqual(n, 1)
        self.assertIsNone(flags[tv])
        self.assertEqual(flags[locked], 1)  # hand-locked roles not requeued
        self.assertEqual(flags[movie], 1)   # movies left alone

    def test_tmdb_page_url(self):
        self.assertEqual(A.tmdb_page_url(19614, "tv"), "https://www.themoviedb.org/tv/19614")
        self.assertEqual(A.tmdb_page_url(481, "movie"), "https://www.themoviedb.org/movie/481")
        self.assertEqual(A.tmdb_page_url(481, None), "https://www.themoviedb.org/movie/481")

if __name__ == "__main__":
    unittest.main()
