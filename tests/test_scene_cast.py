"""Scene casts never store a title as a person: file names split only into known performers, scenecast.py finds and fixes
old title-as-person casts (ThePornDB first, else file name; hand edits kept; unused junk lookups removed), birth-date
queue lookups retry stale 'not found' names and skip titles, export-queue flags scenes without a usable cast. No network.
Run: python tests/test_scene_cast.py"""
import json, os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, tpdb, scenecast, smarttag, birthdates as bd

KNOWN = ["Kelsey Kane", "Katie Kush", "Sera Ryder", "Scarlett Alexis", "Isabella Nice", "Summer Renee"]

def kp(extra=()):
    return tpdb.KnownPerformers(KNOWN + list(extra), title_vocab={"seduce", "your", "baby", "now", "stuck", "stepsister"})

class Parse(unittest.TestCase):
    def test_whole_title_never_a_person(self):
        k = kp()
        p = tpdb.parse_name(r"D:\Porn Scenes\Breeding Material\Breeding Material Kelsey Kane Cum Inside Me.mp4", k)
        self.assertEqual((p["studio"], p["performers"], p["title"]), ("Breeding Material", ["Kelsey Kane"], "Kelsey Kane Cum Inside Me"))
        p = tpdb.parse_name(r"D:\Porn Scenes\Cum 4K\Cum4K Scarlett Alexis Baby I Need Your Cum NOW.mp4", k)
        self.assertEqual((p["studio"], p["performers"]), ("Cum 4K", ["Scarlett Alexis"]))
        p = tpdb.parse_name(r"D:\Porn Scenes\Dad Crush\DadCrush Snoopy Stepdaddy Isabella Nice.mp4", k)
        self.assertEqual((p["studio"], p["performers"]), ("Dad Crush", ["Isabella Nice"]))
        p = tpdb.parse_name(r"D:\Porn Scenes\Family Strokes\familystrokes.18.03.01.jade.amber.mp4", k)
        self.assertEqual((p["studio"], p["date"], p["performers"]), ("Family Strokes", "2018-03-01", []))  # unknown: no person
        p = tpdb.parse_name(r"D:\Porn Scenes\Family Strokes\familystrokes.18.03.01.jade.amber.mp4", kp(["Jade Amber"]))
        self.assertEqual(p["performers"], ["Jade Amber"])
        p = tpdb.parse_name(r"D:\Porn Scenes\Bratty Sis\BrattySis Katie Kush Skyler Storm.mp4", k)
        self.assertEqual(p["performers"], ["Katie Kush"])  # Skyler Storm unknown: kept in the title, not glued to a person
        # without known names: a bare name is one performer, anything longer is not
        self.assertEqual(tpdb.parse_name(r"D:\Porn Scenes\Bratty Sis\BrattySis Lya Cutie.mp4")["performers"], ["Lya Cutie"])
        self.assertEqual(tpdb.parse_name(r"D:\Porn Scenes\Bratty Sis\BrattySis Katie Kush Skyler Storm.mp4")["performers"], [])
        self.assertEqual(tpdb.parse_name(r"D:\Porn Scenes\Dad Crush\DadCrush Teach Me Daddy.mp4")["performers"], [])

    def test_looks_like_title(self):
        k = kp()
        for n in ["Kelsey Kane Cum Inside Me", "Cum4K Scarlett Alexis Baby I Need Your Cum NOW", "familystrokes 18 03 01 jade amber",
                  "Snoopy Stepdaddy Isabella Nice", "Katie Kush Skyler Storm", "Stacy Cruz creampie", "Scene 2"]:
            self.assertTrue(tpdb.looks_like_title(n, k), n)
        for n in ["Kelsey Kane", "Lya Cutie", "Anna De Ville", "Jesse Pony"]:
            self.assertFalse(tpdb.looks_like_title(n, k), n)

    def test_leftover_words(self):
        k = kp()
        self.assertTrue(k.title_only(["Cum", "Inside", "Me"]))
        self.assertFalse(k.title_only(["Skyler", "Storm"]))     # never seen in a title: maybe a performer
        self.assertFalse(k.title_only(["Summer", "Vixen"]))     # a known performer's first name

class Library(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = c = db.connect(); bd.ensure(c)
        for t, cast in [("Matched A", "Kelsey Kane, Katie Kush"), ("Matched B", "Sera Ryder, Isabella Nice, Scarlett Alexis")]:
            c.execute("INSERT INTO films(title,kind,tpdb_id,cast_,updated_at) VALUES(?,?,?,?,?)", (t, "scene", "x" + t[-1], cast, time.time()))
        c.execute("INSERT INTO films(title,kind,primary_genre) VALUES('Stuck On My Stepsister Seduce Your Baby Now','scene','Hardcore')")
        c.commit()

    def add_scene(self, path, cast, edited=None):
        c = self.c
        fid = c.execute("INSERT INTO films(title,kind,cast_,edited_fields,primary_genre) VALUES(?,?,?,?,'Hardcore')",
                        (os.path.basename(path), "scene", cast, edited)).lastrowid
        c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,1)", (fid, path)); c.commit()
        return fid

    def test_scan_never_creates_title_people(self):
        d = tempfile.mkdtemp(); sr = os.path.join(d, "Porn Scenes"); os.makedirs(os.path.join(sr, "Breeding Material"))
        p = os.path.join(sr, "Breeding Material", "BreedingMaterial Kelsey Kane Cum Inside Me.mp4"); open(p, "wb").close()
        q = os.path.join(sr, "Breeding Material", "BreedingMaterial Totally Unknown Person Cum Inside Me.mp4"); open(q, "wb").close()
        c = self.c; db.set_setting(c, "scene_roots", [sr]); c.commit()
        db.scan(c, [sr])
        cast = lambda path: c.execute("SELECT f.cast_ FROM films f JOIN files x ON x.film_id=f.id WHERE x.path=?", (path,)).fetchone()[0]
        self.assertEqual(cast(p), "Kelsey Kane")
        self.assertIsNone(cast(q))
        e = [x for x in db.export_queue(c, [c.execute("SELECT film_id FROM files WHERE path=?", (q,)).fetchone()[0]])][0]
        self.assertEqual(e["cast_missing_birthdate"], [dict(name="(no performers listed)", status="no_cast")])

    def test_fix_junk(self):
        c = self.c
        a = self.add_scene(r"D:\Porn Scenes\Breeding Material\Breeding Material Kelsey Kane Cum Inside Me.mp4", "Breeding Material Kelsey Kane Cum Inside Me")
        b = self.add_scene(r"D:\Porn Scenes\Bratty Sis\BrattySis Katie Kush Skyler Storm.mp4", "Katie Kush Skyler Storm")
        h = self.add_scene(r"D:\Porn Scenes\Dad Crush\DadCrush Snoopy Stepdaddy Isabella Nice.mp4", "Snoopy Stepdaddy Isabella Nice", '["cast_"]')
        u = self.add_scene(r"D:\Porn Scenes\Dad Crush\DadCrush Snoopy Stepdaddy Summer Renee.mp4", "Snoopy Stepdaddy Summer Renee")
        for n in ["Breeding Material Kelsey Kane Cum Inside Me", "Katie Kush Skyler Storm"]:
            c.execute("INSERT INTO birthdate_lookup(name_norm,name,status,at,fb_at) VALUES(?,?,'not_found',?,?)", (db.fold(n), n, time.time(), time.time()))
        c.commit()
        k = db.known_performers(c)
        j = scenecast.junk_entries(c, k)
        self.assertIn(a, j); self.assertIn(b, j); self.assertIn(u, j); self.assertNotIn(h, j)  # hand edit: never touched
        # export flags title-like names; the queue lookup never looks them up
        e = db.export_queue(c, [a])[0]["cast_missing_birthdate"]
        self.assertEqual(e, [dict(name="Breeding Material Kelsey Kane Cum Inside Me", status="title_as_name")])
        self.assertEqual(bd.queue_todo(c, [a], retry=True), [])
        rows = {r["id"]: r for r in scenecast.scene_rows(c, {a, b, u})}
        pa = scenecast.propose(c, rows[a], k)
        self.assertTrue(pa["confident"]); self.assertEqual(pa["new"], ["Kelsey Kane"])
        pb = scenecast.propose(c, rows[b], k)
        self.assertFalse(pb["confident"])  # 'Skyler Storm' unknown: left for review
        cands = [dict(id="s1", title="Stepsis Scene", studio="Bratty Sis", performers=["Katie Kush", "Skyler Storm", "Jay Romero"],
                      score=7, why=["studio", "performers 2/3"], confident=False)]
        pb2 = scenecast.propose(c, rows[b], k, cands)
        self.assertTrue(pb2["confident"]); self.assertEqual(pb2["new"], ["Katie Kush", "Skyler Storm"])  # confirmed by ThePornDB
        sure = [dict(cands[0], confident=True, why=["studio", "date", "title"])]
        pb3 = scenecast.propose(c, rows[b], k, sure)
        self.assertEqual((pb3["new"], pb3["source"]), (["Katie Kush", "Skyler Storm", "Jay Romero"], "tpdb s1"))
        pu = scenecast.propose(c, rows[u], k)
        self.assertFalse(pu["confident"])  # 'Snoopy' never seen in a title
        r = scenecast.apply_plan(c, [pa, pb2, pu, dict(pa, film_id=h, old=["Snoopy Stepdaddy Isabella Nice"])])
        self.assertEqual((r["fixed"], r["skipped"]), ([a, b], [h]))
        self.assertEqual(c.execute("SELECT cast_ FROM films WHERE id=?", (a,)).fetchone()[0], "Kelsey Kane")
        self.assertEqual(c.execute("SELECT cast_ FROM films WHERE id=?", (h,)).fetchone()[0], "Snoopy Stepdaddy Isabella Nice")
        self.assertEqual(c.execute("SELECT cast_ FROM films WHERE id=?", (u,)).fetchone()[0], "Snoopy Stepdaddy Summer Renee")
        self.assertEqual(r["removed_lookups"], 2)
        self.assertEqual(scenecast.apply_plan(c, [pa])["skipped"], [a])  # cast changed since the plan: skipped
        # a date in the file name fills an empty release date (birth-date checks compare it with career years)
        d = self.add_scene(r"D:\Porn Scenes\Family Strokes\familystrokes.18.03.01.kelsey.kane.cum.inside.me.mp4", "familystrokes 18 03 01 kelsey kane cum inside me")
        pd = scenecast.propose(c, scenecast.scene_rows(c, {d})[0], k)
        self.assertEqual((pd["confident"], pd["new"], pd["date"]), (True, ["Kelsey Kane"], "2018-03-01"))
        self.assertEqual(scenecast.apply_plan(c, [pd])["dated"], [d])
        self.assertEqual(tuple(c.execute("SELECT release_date, year FROM films WHERE id=?", (d,)).fetchone()), ("2018-03-01", 2018))

    def test_queue_lookup_retries_stale(self):
        c = self.c
        f = c.execute("INSERT INTO films(title,kind,cast_) VALUES('Q','scene','Old Miss, New Miss, Fresh Name')").lastrowid
        c.execute("INSERT INTO birthdate_lookup(name_norm,name,status,at,fb_at) VALUES('old miss','Old Miss','not_found',?,?)", (time.time() - 30 * 86400,) * 2)
        c.execute("INSERT INTO birthdate_lookup(name_norm,name,status,at,fb_at) VALUES('new miss','New Miss','not_found',?,?)", (time.time(),) * 2)
        c.commit()
        self.assertEqual(bd.queue_todo(c, [f]), [("Old Miss", "full"), ("Fresh Name", "full")])
        self.assertEqual(len(bd.queue_todo(c, [f], retry=True)), 3)

    def test_summary(self):
        q = [dict(cast_missing_birthdate=[dict(name="X Y Cum Inside Me", status="title_as_name")]), dict(cast_missing_birthdate=[])]
        s = smarttag.missing_summary(q)
        self.assertIn("1 of 2 queued film(s)", s); self.assertIn("title stored as a person", s)
        self.assertIn("All 1 queued", smarttag.missing_summary([dict(cast_missing_birthdate=[])]))

if __name__ == "__main__": unittest.main()
