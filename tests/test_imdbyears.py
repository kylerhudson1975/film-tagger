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
    ("tt0086879", "movie", "Amadeus", "Amadeus", 1984),
    ("tt0229208", "tvMovie", "Amadeus", "Amadeus", 1984),
    ("tt7938570", "video", "Amadeus", "Amadeus", 2018),
    ("tt8765432", "movie", "All Grown Up", "All Grown Up", 2018),
    ("tt0118662", "movie", "Ayn Rand: A Sense of Life", "Ayn Rand: A Sense of Life", 1996),
    ("tt0131867", "movie", "Baby Sister", "Baby Sister", 1971),
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
        self.assertEqual(st["rows"], 16)
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

    def test_same_title_prefers_movie_over_video_and_tv(self):
        # 1984 movie + 1984 tvMovie + 2018 video: year 1984 must be the theatrical film, not the video
        hit = iy.find_by_title("Amadeus", year=1984)
        self.assertEqual(hit["tconst"], "tt0086879")
        self.assertEqual(hit["year"], 1984)
        self.assertIn(hit["how"], ("unique_title", "exact_year"))
        self.assertEqual(iy.resolve_film("Amadeus", year=1984)["year"], 1984)

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
        # Isolate from earlier tests in this class that may have stored an imdb_id via apply_movie
        self.c.execute("UPDATE films SET imdb_id=NULL")
        self.c.commit()
        before = {r["id"]: (r["year"], r["release_date"], r["imdb_id"])
                  for r in self.c.execute("SELECT id, year, release_date, imdb_id FROM films")}
        classified = iy.classify_films(self.c)
        after = {r["id"]: (r["year"], r["release_date"], r["imdb_id"])
                 for r in self.c.execute("SELECT id, year, release_date, imdb_id FROM films")}
        self.assertEqual(before, after)
        # No stored IMDb ids: nothing is an apply-able year fix
        self.assertEqual(classified["year_fixes"], [])
        wrong = {r["film_id"]: r for r in classified["wrong_links"]}
        self.assertIn(self.splice, wrong)
        self.assertEqual(wrong[self.splice]["section"], "wrong_link")
        self.assertEqual(wrong[self.splice]["imdb_year"], 2009)
        self.assertEqual(wrong[self.splice]["library"], "Movies")
        self.assertIn(self.hand, wrong)
        self.assertTrue(wrong[self.hand]["hand_edited"])
        self.assertEqual(wrong[self.gift]["imdb_year"], 2015)
        self.assertNotIn(self.matrix, wrong)
        self.assertEqual(wrong[self.classic]["library"], "Classic Porn")
        # scene without an IMDb id is not title-matched
        self.assertNotIn(self.scene, wrong)

    def test_mismatch_api_and_apply_selected(self):
        self.c.execute("UPDATE films SET imdb_id=NULL")
        self.c.commit()
        r = self.cl.get("/api/imdb-years/mismatches").get_json()
        self.assertEqual(r["count"], 0)
        self.assertGreaterEqual(r["wrong_link_count"], 3)
        self.assertIn("Movies", r["wrong_links_by_library"])
        self.assertIn("Classic Porn", r["wrong_links_by_library"])
        # Splice's file is not on disk here: Apply must skip and leave the year alone
        applied = self.cl.post("/api/imdb-years/apply", json={"film_ids": [self.splice]}).get_json()
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["applied"], 0)
        self.assertEqual(applied["skipped"], 1)
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (self.splice,)).fetchone()[0], 2010)
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (self.hand,)).fetchone()[0], 2010)
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (self.classic,)).fetchone()[0], 1971)

    def test_export_csv(self):
        path = os.path.join(_TMP, "mismatches.csv")
        r = self.cl.post("/api/imdb-years/export", json={"path": path}).get_json()
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(path))
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), (r["count"] or 0) + (r.get("wrong_link_count") or 0))
        self.assertIn("section", rows[0])
        titles = {row["title"] for row in rows}
        self.assertIn("Splice", titles)
        self.assertTrue(any(row["title"] == "Splice" and row["section"] == "wrong_link" for row in rows))
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
        d = iy.classify_films(self.c)
        fixes = {r["film_id"]: r for r in d["year_fixes"]}
        wrong = {r["film_id"]: r for r in d["wrong_links"]}
        # Scene title does not match Splice: this stored id is a wrong link, not a year fix
        self.assertNotIn(sid, fixes)
        self.assertIn(sid, wrong)
        self.assertEqual(wrong[sid]["library"], "Porn Scenes")
        self.assertIn("title_mismatch", wrong[sid]["reason"])
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (sid,)).fetchone()[0], 2010)

    def test_stored_id_close_year_is_a_year_fix(self):
        self.c.execute("UPDATE films SET imdb_id=? WHERE id=?", ("tt0443701", self.splice))
        self.c.commit()
        d = iy.classify_films(self.c)
        fixes = {r["film_id"]: r for r in d["year_fixes"]}
        wrong = {r["film_id"]: r for r in d["wrong_links"]}
        self.assertIn(self.splice, fixes)
        self.assertEqual(fixes[self.splice]["section"], "year_fix")
        self.assertEqual(fixes[self.splice]["imdb_year"], 2009)
        self.assertNotIn(self.splice, wrong)

    def test_same_title_different_film_is_wrong_link_not_year_fix(self):
        agu = self._film("All Grown Up", 1995, path=rf"D:\Classic Porn\All Grown Up\agu-{self.uid}.mkv")
        ayn = self._film("Ayn Rand: A Sense of Life", 1998, path=rf"D:\Movies\Ayn Rand\ayn-{self.uid}.mkv")
        baby = self._film("Baby Sister", 1983, path=rf"D:\Movies\Baby Sister\bs-{self.uid}.mkv")
        self.c.commit()
        d = iy.classify_films(self.c)
        fixes = {r["film_id"]: r for r in d["year_fixes"]}
        wrong = {r["film_id"]: r for r in d["wrong_links"]}
        for fid, year, imdb_year in ((agu, 1995, 2018), (ayn, 1998, 1996), (baby, 1983, 1971)):
            self.assertNotIn(fid, fixes)
            self.assertIn(fid, wrong)
            self.assertEqual(wrong[fid]["year"], year)
            self.assertEqual(wrong[fid]["imdb_year"], imdb_year)
            self.assertEqual(wrong[fid]["section"], "wrong_link")
        self.assertEqual(wrong[agu]["library"], "Classic Porn")
        # Apply must refuse these (no stored id / not a ±1 year fix)
        applied = self.cl.post("/api/imdb-years/apply", json={"film_ids": [agu, ayn, baby]}).get_json()
        self.assertEqual(applied["applied"], 0)
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (agu,)).fetchone()[0], 1995)

    def test_apply_movie_does_not_auto_change_a_large_year_gap(self):
        fid = self._film("All Grown Up", 1995, path=rf"D:\Classic Porn\All Grown Up 2\agu2-{self.uid}.mkv")
        self.c.commit()
        A.apply_movie(self.c, fid, {
            "id": 1, "title": "All Grown Up", "imdb_id": "tt8765432",
            "release_date": "1995-01-01", "credits": {},
        })
        self.c.commit()
        row = self.c.execute("SELECT year, imdb_id FROM films WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["year"], 1995)  # not jumped to 2018
        self.assertEqual(row["imdb_id"], "tt8765432")  # id is still stored

    def test_refresh_does_not_hit_network_when_not_stale(self):
        with mock.patch.object(iy, "_download", side_effect=AssertionError("should not download")):
            st = iy.refresh(force=False)
        self.assertTrue(st["ready"])
        self.assertIsNone(st.get("error"))


if __name__ == "__main__":
    unittest.main()
