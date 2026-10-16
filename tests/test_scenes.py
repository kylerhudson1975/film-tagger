"""Scenes libraries: filename parsing, scan as kind='scene' (Hardcore, no TMDB), ThePornDB apply (mocked), filters."""
import json, os, shutil, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, tpdb, app as A

SCENE = {"id": "abc-123", "_id": 628882, "title": "Nikky Thorne & Philippe Soine", "date": "2022-10-16", "duration": 905,
         "description": "desc", "poster": "https://img/p.jpg", "site": {"name": "Beauty And The Senior"},
         "performers": [{"name": "Nikky Thorne"}, {"name": "Philippe Soine"}], "tags": [{"name": "Blonde"}, {"name": "Old/Young"}]}

class Scenes(unittest.TestCase):
    def test_parse(self):
        p = tpdb.parse_name(r"D:\Porn Scenes\Beauty And The Senior\Beauty And The Senior - 2022-10-16 - Nikky Thorne, Philippe Soine - Nikky Thorne & Philippe Soine.mp4")
        self.assertEqual(p, dict(studio="Beauty And The Senior", date="2022-10-16", performers=["Nikky Thorne", "Philippe Soine"], title="Nikky Thorne & Philippe Soine"))
        p = tpdb.parse_name(r"C:\x\BeautyAndTheSenior Nikky Thorne 720p 16102022.mp4")
        self.assertEqual((p["studio"], p["date"], p["performers"]), ("Beauty And The Senior", "2022-10-16", ["Nikky Thorne"]))
        self.assertEqual(tpdb.parse_id("https://theporndb.net/scenes/some-slug-1"), "some-slug-1")
        f = tpdb.scene_fields(SCENE); self.assertEqual((f["runtime"], f["year"], f["tpdb_id"]), (15, 2022, "abc-123"))

    def test_scan_and_apply(self):
        d = tempfile.mkdtemp(); sr = os.path.join(d, "Porn Scenes"); mv = os.path.join(d, "Movies")
        sp = os.path.join(sr, "Beauty And The Senior", "Beauty And The Senior - 2022-10-16 - Nikky Thorne, Philippe Soine - Nikky Thorne & Philippe Soine.mp4")
        os.makedirs(os.path.dirname(sp)); os.makedirs(mv); open(sp, "wb").close(); open(os.path.join(mv, "Alien (1979).mkv"), "wb").close()
        c = db.connect(); db.set_setting(c, "scene_roots", [sr]); c.commit()
        db.scan(c, [mv, sr])
        r = c.execute("SELECT f.* FROM films f JOIN files x ON x.film_id=f.id WHERE x.path=?", (sp,)).fetchone()
        self.assertEqual((r["kind"], r["studio"], r["release_date"], r["year"], r["cast_"], r["title"], r["primary_genre"]),
                         ("scene", "Beauty And The Senior", "2022-10-16", 2022, "Nikky Thorne, Philippe Soine", "Nikky Thorne & Philippe Soine", "Hardcore"))
        m = c.execute("SELECT kind FROM films WHERE title='Alien'").fetchone(); self.assertIsNone(m[0])
        # TMDB refused for scenes
        self.assertIn("ThePornDB", A.fetch_and_apply(c, r["id"], 123) or "")
        cl = A.app.test_client()
        self.assertEqual(cl.post(f"/api/films/{r['id']}/tmdb", json={"tmdb_id": 1}).status_code, 400)
        # ThePornDB load (mocked)
        db.set_setting(c, "tpdb_key", "tok"); c.commit()
        o1, o2 = tpdb.get_scene, tpdb.lookup; tpdb.get_scene = lambda t, s: SCENE; tpdb.lookup = lambda t, p: SCENE
        try:
            cl.put(f"/api/films/{r['id']}", json={"plot": "my plot"})  # hand edit kept
            j = cl.post(f"/api/films/{r['id']}/tpdb", json={"tpdb_id": ""}).get_json()
        finally: tpdb.get_scene, tpdb.lookup = o1, o2
        self.assertEqual((j["runtime"], j["poster"], j["tpdb_id"], j["plot"]), (15, "https://img/p.jpg", "abc-123", "my plot"))
        self.assertEqual({t["name"] for t in j["tags"]}, {"Blonde", "Old/Young"})
        ids = lambda **q: {f["id"] for f in cl.get("/api/films", query_string=q).get_json()["films"]}
        self.assertEqual(ids(scenes=1), {r["id"]}); self.assertNotIn(r["id"], ids(films_only=1))
        self.assertIn(r["id"], ids(studio="Beauty And The Senior"))
        self.assertIn(r["id"], ids(person="Philippe Soine"))
        self.assertEqual(cl.post("/api/films/%d/tpdb" % r["id"], json={"tpdb_id": "bad id!"}).status_code, 400)
        s = cl.get("/api/settings").get_json(); self.assertTrue(s["tpdb_key_set"]); self.assertNotIn("tok", json.dumps(s))
        shutil.rmtree(d, ignore_errors=True)

if __name__ == "__main__": unittest.main()
