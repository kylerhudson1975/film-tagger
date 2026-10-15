"""Regression: AZNude Status. Queue/export/import via db + smarttag.py, POST API, hand edits protected, filter/sort,
and AZNude never changes primary_genre or tags. Temp DB, no network."""
import json, os, sys, tempfile, time, unittest, subprocess
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class AZNudeStatus(unittest.TestCase):
    def setUp(self):
        c = db.connect(); t = time.time()
        self.fid = c.execute("INSERT INTO films(title,original_title,alt_titles,user_akas,year,director,cast_,primary_genre,added_at) VALUES(?,?,?,?,?,?,?,?,?)",
                             (f"AZ Film {t}", "Orig", "Alt One; Alt Two", "My AKA", 1979, "Dir", "A Actor, B Actor", "Drama", t)).lastrowid
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (self.fid, rf"D:\Films\az{self.fid}.mkv"))
        db.add_tag(c, self.fid, "Heist", "Themes"); c.commit(); c.close(); self.cl = A.app.test_client()
    def film(self): return self.cl.get(f"/api/films/{self.fid}").get_json()
    def tags(self): return sorted(t["name"] for t in self.film()["tags"])

    def test_columns_and_default_unchecked(self):
        cols = {r[1] for r in db.connect().execute("PRAGMA table_info(films)")}
        self.assertTrue({"aznude_status", "aznude_checked", "aznude_url", "aznude_note"} <= cols)
        self.assertIsNone(self.film()["aznude_status"])

    def test_queue_export_import_never_touches_genre_or_tags(self):
        tags0 = self.tags()
        self.assertTrue(self.cl.post("/api/aznude/queue", json={"film_ids": [self.fid]}).get_json()["count"] >= 1)
        self.assertTrue(self.film()["aznude_queued"]); self.assertFalse(self.film()["queued"])  # separate from smart queue
        ex = [x for x in db.export_aznude_queue(db.connect()) if x["id"] == self.fid][0]
        self.assertEqual(ex["alt_titles"], ["Alt One", "Alt Two"]); self.assertEqual(ex["akas"], ["My AKA"])
        self.assertEqual(ex["cast"], ["A Actor", "B Actor"]); self.assertEqual(ex["original_title"], "Orig"); self.assertTrue(ex["path"].endswith(".mkv"))
        r = db.import_aznude(db.connect(), [{"film_id": self.fid, "status": "hardcore", "url": "https://www.aznude.com/x", "note": "n"}])
        self.assertEqual(r["set"], 1)
        f = self.film()
        self.assertEqual((f["aznude_status"], f["aznude_url"], f["aznude_note"]), ("Hardcore", "https://www.aznude.com/x", "n"))
        self.assertTrue(f["aznude_checked"]); self.assertFalse(f["aznude_queued"])
        self.assertEqual(f["primary_genre"], "Drama"); self.assertEqual(self.tags(), tags0)
        r = db.import_aznude(db.connect(), [{"film_id": self.fid, "status": "Bogus"}])
        self.assertTrue(r["errors"]); self.assertEqual(self.film()["aznude_status"], "Hardcore")

    def test_hand_edit_protected(self):
        self.cl.put(f"/api/films/{self.fid}", json={"aznude_status": ""})  # unchanged -> not marked
        self.assertNotIn("aznude_status", json.loads(self.film()["edited_fields"] or "[]"))
        self.cl.put(f"/api/films/{self.fid}", json={"aznude_status": "Softcore"})
        f = self.film(); self.assertIn("aznude_status", json.loads(f["edited_fields"])); self.assertTrue(f["aznude_checked"])
        r = db.import_aznude(db.connect(), [{"film_id": self.fid, "status": "Not found", "url": "https://u"}])
        self.assertEqual(r["protected"], [self.fid])
        f = self.film(); self.assertEqual(f["aznude_status"], "Softcore"); self.assertEqual(f["aznude_url"], "https://u")
        self.assertEqual(f["primary_genre"], "Drama")
        self.assertEqual(self.cl.put(f"/api/films/{self.fid}", json={"aznude_status": "nope"}).status_code, 400)

    def test_post_api_and_filter(self):
        r = self.cl.post(f"/api/films/{self.fid}/aznude", json={"status": "Not found", "note": "nothing"})
        self.assertEqual(r.status_code, 200); self.assertEqual(r.get_json()["aznude_status"], "Not found")
        self.assertEqual(self.cl.post(f"/api/films/{self.fid}/aznude", json={"status": "x"}).status_code, 400)
        self.assertEqual(self.cl.post("/api/films/999999/aznude", json={"status": "Hardcore"}).status_code, 404)
        ids = lambda **q: self.cl.get("/api/films", query_string=dict(ids_only=1, **q)).get_json()["ids"]
        self.assertIn(self.fid, ids(aznude="Not found")); self.assertNotIn(self.fid, ids(aznude="Hardcore"))
        self.assertNotIn(self.fid, ids(aznude="(none)"))
        self.assertEqual(self.cl.get("/api/films", query_string=dict(sort="aznude_status")).status_code, 200)
        self.cl.post(f"/api/films/{self.fid}/aznude", json={"status": "Hardcore", "manual": True})
        self.assertIn("aznude_status", json.loads(self.film()["edited_fields"]))
        self.cl.post(f"/api/films/{self.fid}/aznude", json={"status": "Softcore"})
        self.assertEqual(self.film()["aznude_status"], "Hardcore")
        self.cl.post(f"/api/films/{self.fid}/aznude", json={"status": "Softcore", "force": True})
        self.assertEqual(self.film()["aznude_status"], "Softcore"); self.assertEqual(self.film()["primary_genre"], "Drama")

    def test_smarttag_cli(self):
        self.cl.post("/api/aznude/queue", json={"film_ids": [self.fid]})
        d = tempfile.mkdtemp(); out = os.path.join(d, "q.json"); res = os.path.join(d, "r.json")
        env = dict(os.environ, FILMTAGGER_DB=db.DB_PATH)
        subprocess.run([sys.executable, os.path.join(ROOT, "smarttag.py"), "export-aznude-queue", out], check=True, env=env, cwd=ROOT, capture_output=True)
        self.assertIn(self.fid, [x["id"] for x in json.load(open(out, encoding="utf-8"))])
        json.dump([{"film_id": self.fid, "status": "Softcore", "url": "https://a", "note": "brief"}], open(res, "w"))
        subprocess.run([sys.executable, os.path.join(ROOT, "smarttag.py"), "import-aznude", res], check=True, env=env, cwd=ROOT, capture_output=True)
        f = self.film(); self.assertEqual(f["aznude_status"], "Softcore"); self.assertFalse(f["aznude_queued"])

if __name__ == "__main__": unittest.main()
