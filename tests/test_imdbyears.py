"""IMDb release-year check: local title.basics lookup, auto-fix on new TMDB
matches, mismatch review/apply for existing films, Metacritic cache invalidation.
Uses a tiny fake dataset; never talks to datasets.imdbws.com."""
import csv, json, os, sys, tempfile, time, unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ft-imdb-")
os.environ["FILMTAGGER_DB"] = os.path.join(_TMP, "films.db")
os.environ["FILMTAGGER_DATA"] = _TMP
os.environ["FILMTAGGER_NO_IMDB_REFRESH"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db, imdbyears as iy, app as A


FAKE = [
    ("tt0443701", "movie", "Splice", "Splice", 2009),
    ("tt0111161", "movie", "The Shawshank Redemption", "The Shawshank Redemption", 1994),
    ("tt0133093", "movie", "The Matrix", "The Matrix", 1999),
    ("tt0145487", "movie", "Spider-Man", "Spider-Man", 2002),
    ("tt10872600", "movie", "Spider-Man: No Way Home", "Spider-Man: No Way Home", 2021),
    ("tt1234567", "short", "Splice", "Splice", 2005),  # same title; must not steal the movie
    ("tt0000001", "tvSeries", "Ignored Show", "Ignored Show", 2000),  # filtered out
    ("tt0999999", "movie", "No Year", "No Year", r"\N"),  # filtered out
    ("tt1111111", "movie", "The Gift", "The Gift", 2000),
    ("tt2222222", "movie", "The Gift", "The Gift", 2015),
    ("tt3333333", "movie", "Classic Example", "Classic Example", 1972),
    ("tt4444444", "video", "Video Only Title", "Video Only Title", 1985),
]


def _build():
    iy.build_from_rows(FAKE, dest=iy.db_path(), extra_meta={"source_date": "2026-11-10"})


class Helpers(unittest.TestCase):
    def test_parse_imdb_id(self):
        self.assertEqual(iy.parse_imdb_id("tt0443701"), "tt0443701")
        self.assertEqual(iy.parse_imdb_id("https://www.imdb.com/title/tt0443701/"), "tt0443701")
        self.assertEqual(iy.parse_imdb_id("443701"), "tt0443701")
        self.assertIsNone(iy.parse_imdb_id(""))
        self.assertIsNone(iy.parse_imdb_id(None))

    def test_title_key(self):
        self.assertEqual(iy.title_key("Spider-Man"), iy.title_key("Spider Man"))
        self.assertEqual(iy.title_key("Les Démons"), "les demons")

    def test_adjust_release_date(self):
        self.assertEqual(iy.adjust_release_date("2010-06-03", 2010), "2010-06-03")
        self.assertEqual(iy.adjust_release_date("2010-06-03", 2009), "2009")
        self.assertEqual(iy.adjust_release_date(None, 2009), "2009")


class FakeDataset(unittest.TestCase):
    def setUp(self):
        _build()

    def test_status_and_filters(self):
        st = iy.status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["source_date"], "2026-11-10")
        # tvSeries and \N year are dropped; 10 usable rows in FAKE
        self.assertEqual(st["rows"], 10)
        self.assertFalse(st["stale"])
        self.assertIsNone(iy.year_for("tt0000001"))
        self.assertIsNone(iy.year_for("tt0999999"))

    def test_lookup_by_tconst(self):
        self.assertEqual(iy.year_for("tt0443701"), 2009)
        hit = iy.lookup("https://www.imdb.com/title/tt0443701/")
        self.assertEqual(hit["year"], 2009)
        self.assertEqual(hit["how"], "imdb_id")

    def test_unique_title_prefers_movie_over_short(self):
        hit = iy.find_by_title("Splice")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["tconst"], "tt0443701")
        self.assertEqual(hit["year"], 2009)
        self.assertEqual(hit["how"], "unique_title")

    def test_remakes_need_year_and_plus_minus_one(self):
        self.assertIsNone(iy.find_by_title("The Gift"))
        exact = iy.find_by_title("The Gift", year=2000)
        self.assertEqual(exact["tconst"], "tt1111111")
        self.assertEqual(exact["how"], "exact_year")
        # 2014 is uniquely ±1 from 2015 (not from 2000)
        near = iy.find_by_title("The Gift", year=2014)
        self.assertEqual(near["tconst"], "tt2222222")
        self.assertEqual(near["year"], 2015)
        self.assertEqual(near["how"], "year_plus_minus_1")
        # 2008 is not within ±1 of either remake
        self.assertIsNone(iy.find_by_title("The Gift", year=2008))

    def test_video_type_when_no_movie(self):
        hit = iy.find_by_title("Video Only Title")
        self.assertEqual(hit["tconst"], "tt4444444")
        self.assertEqual(hit["title_type"], "video")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        _build()
        self.c = db.connect()
        self._kick = A.kick_metacritic
        self._gal = A.kick_gallery
        self.kicked = []
        A.kick_metacritic = lambda fid, force=False: self.kicked.append((fid, force)) or False
        A.kick_gallery = lambda fid: None
        uid = f"{time.time_ns()}"
        self.uid = uid
        self.splice = self._film("Splice", 2010, path=rf"D:\Movies\Splice\splice-{uid}.mkv",
                                 rd="2010-06-03", ed=json.dumps(["rating"]))
        self.hand = self._film("Splice", 2010, path=rf"D:\Movies\Splice Hand\splice-hand-{uid}.mkv",
                               rd="2010-06-03", ed=json.dumps(["year"]))
        self.gift = self._film("The Gift", 2014, path=rf"D:\Movies\The Gift\gift-{uid}.mkv")
        self.matrix = self._film("The Matrix", 1999, path=rf"D:\Movies\The Matrix\mx-{uid}.mkv")
        self.classic = self._film("Classic Example", 1971, path=rf"D:\Classic Porn\Classic Example\ce-{uid}.mkv")
        self.scene = self._film("Splice", 2010, kind="scene", path=rf"D:\Porn Scenes\Studio\scene-{uid}.mp4")
        # seed a cached Metacritic hit on Splice so apply can clear it
        self.c.execute(
            "UPDATE films SET mc_slug=?, mc_metascore=?, mc_user_score=?, mc_fetched_at=?, mc_match_ver=? WHERE id=?",
            ("splice", 66, 5.7, time.time(), 2, self.splice),
        )
        self.c.commit()
        self.cl = A.app.test_client()

    def tearDown(self):
        A.kick_metacritic = self._kick
        A.kick_gallery = self._gal
        try: self.c.close()
        except Exception: pass

    def _film(self, title, year, kind=None, path=None, rd=None, ed=None, imdb_id=None):
        fid = self.c.execute(
            "INSERT INTO films(title,year,kind,release_date,imdb_id,edited_fields,added_at) VALUES(?,?,?,?,?,?,?)",
            (title, year, kind, rd, imdb_id, ed, time.time()),
        ).lastrowid
        if path:
            self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path))
        return fid

    def test_apply_movie_uses_imdb_year(self):
        A.apply_movie(self.c, self.splice, {
            "id": 13827, "title": "Splice", "imdb_id": "tt0443701",
            "release_date": "2010-06-03", "credits": {},
        })
        self.c.commit()
        row = self.c.execute("SELECT year, release_date, imdb_id FROM films WHERE id=?", (self.splice,)).fetchone()
        self.assertEqual(row["year"], 2009)
        self.assertEqual(row["release_date"], "2009")
        self.assertEqual(row["imdb_id"], "tt0443701")
        # Metacritic cache was cleared because the year changed
        mc = self.c.execute("SELECT mc_slug, mc_metascore, mc_fetched_at FROM films WHERE id=?", (self.splice,)).fetchone()
        self.assertIsNone(mc["mc_slug"])
        self.assertIsNone(mc["mc_metascore"])
        self.assertIsNone(mc["mc_fetched_at"])

    def test_apply_movie_respects_hand_edited_year(self):
        A.apply_movie(self.c, self.hand, {
            "id": 13827, "title": "Splice", "imdb_id": "tt0443701",
            "release_date": "2010-06-03", "credits": {},
        })
        self.c.commit()
        row = self.c.execute("SELECT year, release_date, imdb_id FROM films WHERE id=?", (self.hand,)).fetchone()
        self.assertEqual(row["year"], 2010)
        self.assertEqual(row["release_date"], "2010-06-03")
        self.assertEqual(row["imdb_id"], "tt0443701")  # id is still stored

    def test_mismatch_scan_does_not_change_existing(self):
        before = {r["id"]: (r["year"], r["release_date"], r["imdb_id"])
                  for r in self.c.execute("SELECT id, year, release_date, imdb_id FROM films")}
        rows = iy.find_mismatches(self.c)
        after = {r["id"]: (r["year"], r["release_date"], r["imdb_id"])
                 for r in self.c.execute("SELECT id, year, release_date, imdb_id FROM films")}
        self.assertEqual(before, after)
        by_id = {r["film_id"]: r for r in rows}
        self.assertIn(self.splice, by_id)
        self.assertEqual(by_id[self.splice]["year"], 2010)
        self.assertEqual(by_id[self.splice]["imdb_year"], 2009)
        self.assertEqual(by_id[self.splice]["imdb_id"], "tt0443701")
        self.assertEqual(by_id[self.splice]["library"], "Movies")
        self.assertFalse(by_id[self.splice]["hand_edited"])
        # hand-edited year is listed but flagged
        self.assertIn(self.hand, by_id)
        self.assertTrue(by_id[self.hand]["hand_edited"])
        # remake ±1
        self.assertEqual(by_id[self.gift]["imdb_year"], 2015)
        self.assertEqual(by_id[self.gift]["year"], 2014)
        # matching year is not listed
        self.assertNotIn(self.matrix, by_id)
        # Classic Porn unique title is included
        self.assertEqual(by_id[self.classic]["library"], "Classic Porn")
        self.assertEqual(by_id[self.classic]["imdb_year"], 1972)
        # scene without an IMDb id is not title-matched
        self.assertNotIn(self.scene, by_id)

    def test_mismatch_api_and_apply_selected(self):
        r = self.cl.get("/api/imdb-years/mismatches").get_json()
        self.assertGreaterEqual(r["count"], 3)
        self.assertIn("Movies", r["by_library"])
        self.assertIn("Classic Porn", r["by_library"])
        # apply only Splice (not the hand-edited copy, not classic)
        applied = self.cl.post("/api/imdb-years/apply", json={"film_ids": [self.splice]}).get_json()
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["applied"], 1)
        self.assertEqual(len(applied["changed"]), 1)
        row = self.c.execute("SELECT year, release_date, imdb_id, mc_slug, mc_metascore FROM films WHERE id=?", (self.splice,)).fetchone()
        self.assertEqual(row["year"], 2009)
        self.assertEqual(row["release_date"], "2009")
        self.assertEqual(row["imdb_id"], "tt0443701")
        self.assertIsNone(row["mc_slug"])
        self.assertIsNone(row["mc_metascore"])
        self.assertIn((self.splice, True), self.kicked)
        # others unchanged
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (self.hand,)).fetchone()[0], 2010)
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (self.classic,)).fetchone()[0], 1971)

    def test_export_csv(self):
        path = os.path.join(_TMP, "mismatches.csv")
        r = self.cl.post("/api/imdb-years/export", json={"path": path}).get_json()
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(path))
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), r["count"])
        titles = {row["title"] for row in rows}
        self.assertIn("Splice", titles)
        self.assertNotIn("The Matrix", titles)

    def test_status_api(self):
        r = self.cl.get("/api/imdb-years/status").get_json()
        self.assertTrue(r["ready"])
        self.assertEqual(r["source_date"], "2026-11-10")

    def test_scene_with_imdb_id_is_listed_not_auto_changed(self):
        sid = self._film("Some Scene", 2010, kind="scene",
                         path=rf"D:\Porn Scenes\Studio\with-id-{self.uid}.mp4",
                         imdb_id="tt0443701")
        self.c.commit()
        rows = {r["film_id"]: r for r in iy.find_mismatches(self.c)}
        self.assertIn(sid, rows)
        self.assertEqual(rows[sid]["library"], "Porn Scenes")
        self.assertEqual(rows[sid]["imdb_year"], 2009)
        # scan still did not write
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (sid,)).fetchone()[0], 2010)

    def test_refresh_does_not_hit_network_when_not_stale(self):
        with mock.patch.object(iy, "_download", side_effect=AssertionError("should not download")):
            st = iy.refresh(force=False)
        self.assertTrue(st["ready"])
        self.assertIsNone(st.get("error"))


if __name__ == "__main__":
    unittest.main()
