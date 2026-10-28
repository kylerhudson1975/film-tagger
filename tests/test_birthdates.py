"""Birth dates from Wikidata/Wikipedia: confident results saved with a source, hand edits / existing dates never
overwritten, ThePornDB/TMDB refreshes without a date keep the found one, queue order, review 'none'; fallback sites
(stubbed): confident hit saved with site + URL, uncorroborated page -> review with link, earlier misses re-queued once. No network.
Run: python tests/test_birthdates.py"""
import os, sys, json, tempfile
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, birthdates as bd
c = db.connect(); bd.ensure(c)
assert not bd.enabled()  # temp DB: never online
for t, cast, pg in [("A", "Lina Test, Hand Edited", "Hardcore"), ("B", "Lina Test, Main Stream", "Drama"), ("C", "Main Stream", "Drama")]:
    c.execute("INSERT INTO films(title,year,cast_,primary_genre) VALUES(?,1975,?,?)", (t, cast, pg))
c.execute("INSERT INTO people(name,name_norm,edited_fields) VALUES('Hand Edited','hand edited','[\"birthday\"]')"); c.commit()
q = bd.queue(c); assert q[0] == "Lina Test" and "Hand Edited" not in q, q
bd.lookup = lambda name, *a, **k: dict(status="filled", birthday="1954-06-25", source="wikidata Q1", detail="test")
assert bd.lookup_one(c, "Lina Test")["status"] == "filled"
r = c.execute("SELECT * FROM people WHERE name_norm='lina test'").fetchone(); assert r["birthday"] == "1954-06-25" and r["birth_date_source"] == "wikidata Q1"
assert bd.lookup_one(c, "Hand Edited")["status"] == "skipped"
assert bd.lookup_one(c, "Lina Test")["status"] == "skipped"  # already has one
# ThePornDB refresh with no birthday keeps the Wikidata one; with a birthday marks the source
people.apply_tpdb(c, r, {"id": "x1", "name": "Lina Test", "extras": {}})
assert c.execute("SELECT birthday FROM people WHERE id=?", (r["id"],)).fetchone()[0] == "1954-06-25"
cl = app.app.test_client()
assert cl.get(f"/api/person/{r['id']}").json["person"]["birth_date_source"] == "wikidata Q1"
st = cl.get("/api/birthdates/status").json; assert st["totals"]["filled"] == 1 and st["totals"]["saved"] == 1
bd.lookup = lambda name, *a, **k: dict(status="ambiguous", detail=json.dumps([{"qid": "Q2", "birthday": "1950-01-01"}, {"qid": "Q3", "birthday": None}]))
bd.lookup_one(c, "Main Stream")
rv = cl.get("/api/birthdates/review").json["review"]; assert [x["name"] for x in rv] == ["Main Stream"] and len(rv[0]["candidates"]) == 2
assert cl.post("/api/birthdates/review", json={"key": "main stream", "qid": ""}).json["ok"]
assert cl.get("/api/birthdates/review").json["review"] == []
cl.post(f"/api/person/{r['id']}/edit", json={"birthday": "1954-06-26"})
assert c.execute("SELECT birth_date_source FROM people WHERE id=?", (r["id"],)).fetchone()[0] == "hand edit"
# a date already found (e.g. TMDB) typed again by hand with a note: protected, noted, and off the review list
c.execute("INSERT INTO films(title,year,cast_,primary_genre) VALUES('D',2023,'Moon Test','Hardcore')")
c.execute("INSERT INTO people(name,name_norm,birthday,birth_date_source) VALUES('Moon Test','moon test','1999-04-08','tmdb person 9')"); c.commit()
bd._record(c, "Moon Test", dict(status="ambiguous", detail=json.dumps([{"site": "freeones", "label": "FreeOnes", "url": "https://www.freeones.com/x", "birthday": "1999-04-08"}]))); c.commit()
assert cl.get("/api/birthdates/review").json["review"] == []  # has a birth date now: not listed
c.execute("UPDATE people SET birthday=NULL WHERE name_norm='moon test'"); c.commit()
assert [x["name"] for x in cl.get("/api/birthdates/review").json["review"]] == ["Moon Test"]
c.execute("UPDATE people SET birthday='1999-04-08' WHERE name_norm='moon test'"); c.commit()
mp = c.execute("SELECT id FROM people WHERE name_norm='moon test'").fetchone()[0]
assert cl.post(f"/api/person/{mp}/edit", json={"birthday": "1999-04-08", "birth_date_note": "entered by Kyle"}).json["edited"] == ["birthday"]
m = c.execute("SELECT * FROM people WHERE id=?", (mp,)).fetchone()
assert m["birthday"] == "1999-04-08" and m["birth_date_source"] == "hand edit (entered by Kyle)"
assert c.execute("SELECT status FROM birthdate_lookup WHERE name_norm='moon test'").fetchone()[0] == "skipped"
assert cl.post(f"/api/person/{mp}/edit", json={"birthday": "1999-04-08"}).json["ok"]  # same date again, no note: note kept
assert c.execute("SELECT birth_date_source FROM people WHERE id=?", (mp,)).fetchone()[0] == "hand edit (entered by Kyle)"
# Dashboard 'People without a birth date': a person whose only title left the library is flagged, not shown as in it
import reports
c.execute("INSERT INTO people(name,name_norm) VALUES('Gone Test','gone test')"); c.execute("INSERT INTO people(name,name_norm) VALUES('Still Here','still here')")
c.execute("INSERT INTO films(title,year,cast_,primary_genre) VALUES('E',2024,'Still Here','Drama')"); c.commit()
nb = {x["name"]: x["in_library"] for x in reports.dashboard(c)["no_birthday_people"]}
assert nb.get("Gone Test") is False and nb.get("Still Here") is True, nb
print("ok")

# ---------- fallback sites (Boobpedia / FreeOnes / Babepedia): stubbed, no network ----------
import bdsources as bs
assert bs.parse_date("Thursday 4th of August 1955") == bs.parse_date("August 4, 1955") == "1955-08-04"
pg = dict(name="Lina Two", aliases=["Lina Deux"], birthday="1955-08-04", start=1976, end=1980, text="")
assert bs.check(pg, ["Lina Two"], [1977, 1979], [])[0]
assert bs.check(pg, ["Lina Deux"], [1961, 1977], [])[0]                       # alias match; an outlier credit year is fine
assert not bs.check(pg, ["Lina Two"], [], [])[0]                              # nothing in the DB to corroborate -> review
assert not bs.check(pg, ["Lina Two"], [1999], [])[0]                          # career years don't fit
assert not bs.check(pg, ["Lina Two"], [1977], [], wd_year=1956)[0]            # Wikidata's year disagrees
assert not bs.check(dict(pg, name="Lina"), ["Lina"], [1977], [])[0]           # one-word names never auto-save
assert not bs.check(pg, ["Other Name"], [1977], [])[0]
bd.FB_FORCE[0] = True
c.execute("INSERT INTO films(title,year,cast_,primary_genre) VALUES('D',1977,'Lina Two, Nora Three','Hardcore')"); c.commit()
q = bd.queue(c, with_recheck=True); assert ("Lina Two", False) in q, q
bd.lookup = lambda name, *a, **k: dict(status="not_found", detail="0 Wikidata results")
bs.lookup = lambda names, years, titles, wd_year=None, urls=None: dict(
    hit=dict(site="boobpedia", label="Boobpedia", url="https://www.boobpedia.com/boobs/Lina_Two", birthday="1955-08-04", name="Lina Two", why="career fits") if "Lina Two" in names else None,
    cands=[] if "Lina Two" in names else [dict(site="freeones", label="FreeOnes", url="https://www.freeones.com/nora-three/bio", birthday="1960-01-02", name="Nora Three", why="no credit years here to compare")],
    tried=["Boobpedia", "FreeOnes", "Babepedia"], skipped={})
assert bd.lookup_one(c, "Lina Two")["status"] == "filled"
r2 = c.execute("SELECT * FROM people WHERE name_norm='lina two'").fetchone()
assert r2["birthday"] == "1955-08-04" and r2["birth_date_source"] == "boobpedia https://www.boobpedia.com/boobs/Lina_Two", dict(r2)
assert bd.lookup_one(c, "Nora Three")["status"] == "ambiguous"
rv = cl.get("/api/birthdates/review").json["review"]; nr = [x for x in rv if x["name"] == "Nora Three"][0]
assert nr["candidates"][0]["url"].startswith("https://www.freeones.com/") and "iafd.com" in nr["iafd_search"]
assert cl.post("/api/birthdates/review", json={"key": "nora three", "url": "https://evil.example/x"}).status_code == 400
assert cl.post("/api/birthdates/review", json={"key": "nora three", "url": "https://www.freeones.com/nora-three/bio"}).json["ok"]
assert c.execute("SELECT birthday, birth_date_source FROM people WHERE name_norm='nora three'").fetchone()[0] == "1960-01-02"
# earlier misses are re-queued once for the fallback sites (only those, not Wikidata again)
c.execute("INSERT INTO films(title,year,cast_,primary_genre) VALUES('E',1978,'Old Miss','Hardcore')")
bd._record(c, "Old Miss", dict(status="not_found", detail="0 Wikidata results")); c.commit()
assert ("Old Miss", True) in bd.queue(c, with_recheck=True) and "Old Miss" not in bd.queue(c)
bd.lookup = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Wikidata asked again"))
res = bd.lookup_one(c, "Old Miss", recheck=True); assert res["status"] == "ambiguous" or res["status"] == "not_found", res
assert c.execute("SELECT fb_at FROM birthdate_lookup WHERE name_norm='old miss'").fetchone()[0]
assert ("Old Miss", True) not in bd.queue(c, with_recheck=True)
st = cl.get("/api/birthdates/status").json; assert st["totals"]["by_source"].get("boobpedia") == 1 and len(st["sites"]) == 3, st
# hand edits are never overwritten by a fallback hit
assert bd.lookup_one(c, "Hand Edited")["status"] == "skipped"
print("fallback ok")
