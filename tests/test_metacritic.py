"""Metacritic Metascore + User score: search+stats lookup, HTML fallback, cache, Movies-only
scope, manual slug, soft failures, and API. HTTP is always mocked."""
import json, os, sys, tempfile, time, unittest
from unittest import mock

if "FILMTAGGER_DB" not in os.environ:
    os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db, metacritic as mc, app as A

SEARCH_HIT = {
    "data": {"items": [
        {"title": "Project Hail Mary", "type": "movie", "slug": "project-hail-mary", "premiereYear": 2026},
        {"title": "Project Hail Mary", "type": "movie", "slug": "project-hail-mary-1950", "premiereYear": 1950},
    ]}
}
CRITIC = {"data": {"item": {"score": 77, "reviewCount": 60, "max": 100, "sentiment": "Generally favorable"}}}
USER = {"data": {"item": {"score": 8.2, "reviewCount": 1347, "max": 10, "sentiment": "Universal acclaim"}}}
PAGE_HTML = """<!doctype html><html><head>
<script type="application/ld+json">{"@type":"Movie","name":"Fargo",
"aggregateRating":{"@type":"AggregateRating","name":"Metascore","ratingValue":85,"reviewCount":25}}</script>
<script type="application/json" id="__NUXT_DATA__">[]</script>
</head><body>
<div title="User score 8.4 out of 10" data-testid="global-score-value-wrapper">8.4</div>
<a href="/movie/fargo/user-reviews/">Based on 412 User Ratings</a>
<a href="/movie/fargo/critic-reviews/">Based on 25 Critic Reviews</a>
</body></html>"""


def _http(url, accept="*/*"):
    if "/finder/metacritic/search/" in url:
        return 200, url, json.dumps(SEARCH_HIT)
    if "/critic/movies/" in url and "/stats/" in url:
        return 200, url, json.dumps(CRITIC)
    if "/user/movies/" in url and "/stats/" in url:
        return 200, url, json.dumps(USER)
    if "metacritic.com/movie/" in url:
        return 404, url, ""
    return 404, url, ""


class Lookup(unittest.TestCase):
    def setUp(self):
        mc.set_rate_gap(0)
        self.patch = mock.patch.object(mc, "http_get", side_effect=_http)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        mc.set_rate_gap(1)

    def test_title_year_match(self):
        r = mc.lookup("Project Hail Mary", 2026)
        self.assertTrue(r["ok"])
        self.assertEqual(r["slug"], "project-hail-mary")
        self.assertEqual(r["metascore"], 77)
        self.assertEqual(r["critic_count"], 60)
        self.assertEqual(r["user_score"], 8.2)
        self.assertEqual(r["user_count"], 1347)
        self.assertIn("search", r["methods"])
        self.assertIn("stats", r["methods"])

    def test_wrong_year_does_not_guess(self):
        r = mc.lookup("Project Hail Mary", 1999)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "not_found")

    def test_known_slug_skips_search(self):
        calls = []
        def http(url, accept="*/*"):
            calls.append(url)
            return _http(url, accept)
        with mock.patch.object(mc, "http_get", side_effect=http):
            r = mc.lookup("Ignored Title", 1900, slug="project-hail-mary")
        self.assertTrue(r["ok"])
        self.assertEqual(r["user_score"], 8.2)
        self.assertTrue(any("/stats/" in u for u in calls))
        self.assertFalse(any("/search/" in u for u in calls))

    def test_parse_slug_from_url(self):
        self.assertEqual(mc.parse_slug("https://www.metacritic.com/movie/project-hail-mary/user-reviews/"), "project-hail-mary")
        self.assertEqual(mc.parse_slug("Project Hail Mary"), "project-hail-mary")
        self.assertIsNone(mc.parse_slug(""))

    def test_html_fallback_when_stats_fail(self):
        def http(url, accept="*/*"):
            if "/search/" in url:
                return 200, url, json.dumps({"data": {"items": [{"title": "Fargo", "type": "movie", "slug": "fargo", "premiereYear": 1996}]}})
            if "/stats/" in url:
                return 500, url, ""
            if url.endswith("/movie/fargo/") or "/movie/fargo" in url:
                return 200, url, PAGE_HTML
            return 404, url, ""
        with mock.patch.object(mc, "http_get", side_effect=http):
            r = mc.lookup("Fargo", 1996)
        self.assertTrue(r["ok"])
        self.assertEqual(r["slug"], "fargo")
        self.assertEqual(r["metascore"], 85)
        self.assertEqual(r["critic_count"], 25)
        self.assertEqual(r["user_score"], 8.4)
        self.assertEqual(r["user_count"], 412)
        self.assertTrue(any(m in r["methods"] for m in ("json-ld", "html-label")))

    def test_network_error_is_soft(self):
        def boom(url, accept="*/*"):
            raise OSError("down")
        # lookup catches around the whole body; http_get raising in _get_json is caught per-request
        # and search returns None -> not_found. Force a raise after search by blowing up stats and page.
        def http(url, accept="*/*"):
            if "/search/" in url:
                return 200, url, json.dumps({"data": {"items": [{"title": "X", "type": "movie", "slug": "x", "premiereYear": 2020}]}})
            raise OSError("down")
        with mock.patch.object(mc, "http_get", side_effect=http):
            r = mc.lookup("X", 2020)
        self.assertFalse(r["ok"])  # no scores, treated as not_found (scores_from_* swallow exceptions)
        self.assertIn(r["reason"], ("not_found", "error"))


class Api(unittest.TestCase):
    def setUp(self):
        mc.set_rate_gap(0)
        self._kick = A.kick_metacritic
        self._gal = A.kick_gallery
        A.kick_metacritic = lambda fid, force=False: False  # no background threads in API tests
        A.kick_gallery = lambda fid: None
        self._lookup = mc.lookup
        mc.lookup = lambda title, year=None, slug=None: {
            "ok": True, "slug": slug or "project-hail-mary", "metascore": 77,
            "critic_count": 60, "user_score": 8.2, "user_count": 1347, "methods": ["search", "stats"],
        }
        c = self.c = db.connect()
        uid = f"{time.time_ns()}"
        mk = lambda title, year, kind=None, path=None, ed=None: self._film(c, title, year, kind, path, ed)
        self.movie = mk("Project Hail Mary", 2026, path=rf"D:\Movies\Project Hail Mary (2026)\phm-{uid}.mkv")
        self.scene = mk("Some Scene", 2024, kind="scene", path=rf"D:\Porn Scenes\Studio\scene-{uid}.mp4")
        self.classic = mk("Deep Throat", 1972, path=rf"D:\Classic Porn\Deep Throat\dt-{uid}.mkv")
        c.commit()
        self.cl = A.app.test_client()

    def tearDown(self):
        A.kick_metacritic = self._kick
        A.kick_gallery = self._gal
        mc.lookup = self._lookup
        mc.set_rate_gap(1)
        try: self.c.close()
        except Exception: pass
        # Don't leave a backfill job running into the next test
        if getattr(A, "JOB", None) and A.JOB.get("running"):
            A.JOB["cancel"] = True
            for _ in range(50):
                if not A.JOB.get("running"): break
                time.sleep(0.02)

    def _film(self, c, title, year, kind, path, ed):
        fid = c.execute(
            "INSERT INTO films(title,year,kind,edited_fields,added_at) VALUES(?,?,?,?,?)",
            (title, year, kind, ed, time.time()),
        ).lastrowid
        if path:
            c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path))
        return fid

    def test_schema_columns_exist(self):
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(films)")}
        for col in ("mc_slug", "mc_metascore", "mc_critic_count", "mc_user_score", "mc_user_count", "mc_fetched_at"):
            self.assertIn(col, cols)

    def test_refresh_saves_scores_and_urls(self):
        r = A.refresh_metacritic(self.c, self.movie, force=True)
        self.assertTrue(r["ok"])
        j = self.cl.get(f"/api/films/{self.movie}").get_json()
        self.assertEqual(j["mc_slug"], "project-hail-mary")
        self.assertEqual(j["mc_metascore"], 77)
        self.assertEqual(j["mc_critic_count"], 60)
        self.assertAlmostEqual(j["mc_user_score"], 8.2)
        self.assertEqual(j["mc_user_count"], 1347)
        self.assertTrue(j["mc_eligible"])
        self.assertEqual(j["mc_critic_url"], "https://www.metacritic.com/movie/project-hail-mary/critic-reviews/")
        self.assertEqual(j["mc_user_url"], "https://www.metacritic.com/movie/project-hail-mary/user-reviews/")
        self.assertFalse(j.get("mc_pending"))

    def test_skips_scenes_and_classic_porn(self):
        self.assertFalse(db.is_metacritic_film(self.c, self.scene))
        self.assertFalse(db.is_metacritic_film(self.c, self.classic))
        self.assertTrue(db.is_metacritic_film(self.c, self.movie))
        self.assertEqual(A.refresh_metacritic(self.c, self.scene, force=True)["reason"], "skipped")
        self.assertEqual(A.refresh_metacritic(self.c, self.classic, force=True)["reason"], "skipped")
        js = self.cl.post(f"/api/films/{self.scene}/metacritic").get_json()
        self.assertTrue(js.get("skipped"))
        jc = self.cl.get(f"/api/films/{self.classic}").get_json()
        self.assertFalse(jc["mc_eligible"])
        self.assertIsNone(jc.get("mc_metascore") or None)

    def test_cache_skips_second_lookup(self):
        calls = {"n": 0}
        real = mc.lookup
        def counted(title, year=None, slug=None):
            calls["n"] += 1
            return real(title, year, slug)
        mc.lookup = counted
        A.refresh_metacritic(self.c, self.movie, force=True)
        self.assertEqual(calls["n"], 1)
        A.refresh_metacritic(self.c, self.movie, force=False)
        self.assertEqual(calls["n"], 1)  # still cached
        # stale: older than 30 days
        self.c.execute("UPDATE films SET mc_fetched_at=? WHERE id=?", (time.time() - A.MC_CACHE_SEC - 10, self.movie))
        self.c.commit()
        A.refresh_metacritic(self.c, self.movie, force=False)
        self.assertEqual(calls["n"], 2)

    def test_manual_slug_is_used_and_kept(self):
        def lookup(title, year=None, slug=None):
            self.assertEqual(slug, "fargo")
            return {"ok": True, "slug": "fargo", "metascore": 85, "critic_count": 25, "user_score": 8.4, "user_count": 412, "methods": ["stats"]}
        mc.lookup = lookup
        j = self.cl.put(f"/api/films/{self.movie}", json={"mc_slug": "https://www.metacritic.com/movie/fargo/"}).get_json()
        self.assertIn("mc_slug", json.loads(j["edited_fields"]))
        self.assertEqual(j["mc_slug"], "fargo")
        # Even if lookup wanted to return a different slug, refresh keeps the hand-set one:
        # (already fargo). A later auto search must not run while it's manual: refresh uses slug.
        A.refresh_metacritic(self.c, self.movie, force=True)
        row = self.c.execute("SELECT mc_slug, edited_fields FROM films WHERE id=?", (self.movie,)).fetchone()
        self.assertEqual(row["mc_slug"], "fargo")
        self.assertIn("mc_slug", json.loads(row["edited_fields"]))

    def test_not_found_is_silent_and_cached(self):
        mc.lookup = lambda *a, **k: {"ok": False, "reason": "not_found"}
        r = A.refresh_metacritic(self.c, self.movie, force=True)
        self.assertEqual(r["reason"], "not_found")
        j = self.cl.get(f"/api/films/{self.movie}").get_json()
        self.assertIsNone(j.get("mc_metascore"))
        self.assertIsNone(j.get("mc_user_score"))
        self.assertFalse(j.get("error"))
        self.assertIsNotNone(j.get("mc_fetched_at"))
        # second refresh is cached, no lookup
        calls = {"n": 0}
        mc.lookup = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"ok": False, "reason": "not_found"}
        A.refresh_metacritic(self.c, self.movie, force=False)
        self.assertEqual(calls["n"], 0)

    def test_get_film_kicks_when_missing(self):
        kicked = []
        A.kick_metacritic = lambda fid, force=False: kicked.append((fid, force)) or True
        j = self.cl.get(f"/api/films/{self.movie}").get_json()
        self.assertEqual(kicked, [(self.movie, False)])
        self.assertTrue(j["mc_pending"])
        kicked.clear()
        A.kick_metacritic = lambda fid, force=False: kicked.append(fid) or False
        self.cl.get(f"/api/films/{self.scene}")
        self.assertEqual(kicked, [self.scene])  # kick is called; it will no-op inside for scenes if unpatched
        # with the real kick, scenes are skipped
        A.kick_metacritic = self._kick
        self.assertFalse(A.kick_metacritic(self.scene))
        self.assertFalse(A.kick_metacritic(self.classic))

    def test_backfill_endpoint_skips_ineligible_and_accepts_limit(self):
        j = self.cl.post("/api/metacritic/start", json={"limit": 1, "film_ids": [self.movie, self.scene, self.classic]}).get_json()
        self.assertEqual(j["kind"], "metacritic")
        self.assertEqual(j["total"], 1)  # only the mainstream movie
        # let the tiny job finish
        for _ in range(50):
            if not A.JOB.get("running"): break
            time.sleep(0.02)
        self.assertFalse(A.JOB.get("running"))
        row = self.c.execute("SELECT mc_metascore, mc_user_score FROM films WHERE id=?", (self.movie,)).fetchone()
        self.assertEqual(row["mc_metascore"], 77)
        self.assertAlmostEqual(row["mc_user_score"], 8.2)
        # scenes / classic unchanged
        self.assertIsNone(self.c.execute("SELECT mc_fetched_at FROM films WHERE id=?", (self.scene,)).fetchone()[0])
        self.assertIsNone(self.c.execute("SELECT mc_fetched_at FROM films WHERE id=?", (self.classic,)).fetchone()[0])

    def test_tmdb_does_not_clear_scores(self):
        A.refresh_metacritic(self.c, self.movie, force=True)
        self.c.execute("UPDATE films SET tmdb_id=1 WHERE id=?", (self.movie,))
        self.c.commit()
        A.apply_movie(self.c, self.movie, {"id": 1, "title": "Project Hail Mary", "release_date": "2026-03-20", "credits": {}})
        self.c.commit()
        row = self.c.execute("SELECT mc_slug, mc_metascore, mc_user_score FROM films WHERE id=?", (self.movie,)).fetchone()
        self.assertEqual(row["mc_slug"], "project-hail-mary")
        self.assertEqual(row["mc_metascore"], 77)
        self.assertAlmostEqual(row["mc_user_score"], 8.2)


if __name__ == "__main__":
    unittest.main()
