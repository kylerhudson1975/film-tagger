"""Ethnicity, more sources and quick tagging: the backfill's ThePornDB name search (only one exact name / alias match, never
when its birth date disagrees; the link itself is not stored), TMDB external_ids -> Wikidata QID / IMDb id -> P172, coverage
by side, and the Needs ethnicity queue (photo / most credits first, Skip remembered until reset, tag = protected 'manual'
hand edit, Undo puts the person back). Run: python tests/test_ethnicity_more.py"""
import os, sys, io, json, time, tempfile, urllib.request, urllib.parse
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app, tpdb, birthdates, people as P
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "tmdb_key", "k" * 32); c.commit()
ppl = [("Gia Name", 1, None), ("Hal Twin", 1, None), ("Ivy Born", 1, "1990-01-01"), ("Kim Many", 3, None),
       ("Jo Ext", 0, None), ("Lu Qid", 0, None), ("Mo None", 0, None)]
fid = 0
for n, scenes, bd in ppl:
    for k in range(max(scenes, 1)):
        fid += 1; adult = scenes > 0
        c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(?,?,?,?)", (fid, f"T{fid}", "scene" if adult else None, n))
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, rf"D:\S\{fid}.mp4" if adult else rf"D:\M\{fid}.mkv"))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n, _, _ in ppl}
for n, _, bd in ppl:
    if bd: c.execute("UPDATE people SET birthday=? WHERE id=?", (bd, ids[n])); c.commit()
for n, t in (("Jo Ext", 601), ("Lu Qid", 602), ("Mo None", 603)): c.execute("UPDATE people SET tmdb_person_id=? WHERE id=?", (t, ids[n])); c.commit()
c.execute("UPDATE people SET profile_path='/x.jpg' WHERE id=?", (ids["Mo None"],)); c.commit()
# ThePornDB search results
S = {"Gia Name": [{"id": "g1", "name": "Gia Name", "extras": {"ethnicity": "Asian", "birthday": "1991-02-02"}}, {"id": "g2", "name": "Gia Namely", "extras": {"ethnicity": "White"}}],
     "Hal Twin": [{"id": "h1", "name": "Hal Twin", "extras": {"ethnicity": "Black"}}, {"id": "h2", "name": "Hal Twin", "extras": {"ethnicity": "White"}}],
     "Ivy Born": [{"id": "i1", "name": "Ivy Born", "extras": {"ethnicity": "Latin", "birthday": "1985-05-05"}}],
     "Kim Many": [{"id": "k1", "name": "Kimberly", "aliases": ["Kim Many"], "extras": {}}]}
calls = []
def fake_tpdb(path, tok, **k):
    calls.append((path, k.get("q")))
    if path == "/performers": return {"data": S.get(k.get("q"), [])}
    if path == "/performers/k1": return {"data": {"id": "k1", "extras": {"ethnicity": "Middle Eastern"}}}
    raise AssertionError(path)
tpdb.call_retry = fake_tpdb
def fake_sparql(q):
    b = []
    if "P4985" in q: return {"results": {"bindings": []}}  # none of their TMDB ids are on Wikidata
    if "P345" in q and '"nm601"' in q: b = [{"k": {"value": "nm601"}, "p": {"value": "http://www.wikidata.org/entity/Q61"}, "eLabel": {"value": "Asian Americans"}}]
    if "wd:Q62" in q: b = [{"k": {"value": "http://www.wikidata.org/entity/Q62"}, "p": {"value": "http://www.wikidata.org/entity/Q62"}, "eLabel": {"value": "Irish Americans"}}]
    return {"results": {"bindings": b}}
birthdates._sparql = fake_sparql
X = {601: {"imdb_id": "nm601"}, 602: {"wikidata_id": "Q62", "imdb_id": "nm602"}, 603: {}}
def fake_url(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    assert "/external_ids" in url, url
    pid = int(urllib.parse.urlparse(url).path.split("/")[-2])
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return R(json.dumps(X[pid]).encode())
urllib.request.urlopen = fake_url
P_ = P
cl.post("/api/people/ethnicity_job")
for _ in range(400):
    st = cl.get("/api/people/ethnicity_job").json
    if not st["running"]: break
    time.sleep(0.05)
E = lambda n: tuple(c.execute("SELECT ethnicity, ethnicity_source FROM people WHERE id=?", (ids[n],)).fetchone())
assert E("Gia Name") == ("Asian", "theporndb g1 (name match)"), E("Gia Name")
assert E("Hal Twin") == (None, None) and E("Ivy Born") == (None, None)  # two exact matches / birth date disagrees
assert E("Kim Many") == ("Middle Eastern", "theporndb k1 (name match)"), E("Kim Many")  # alias match, short result -> performer read
assert E("Jo Ext") == ("Asian", "wikidata Q61 (via IMDb id)"), E("Jo Ext")
assert E("Lu Qid") == ("White", "wikidata Q62 (via TMDB)"), E("Lu Qid")
assert E("Mo None") == (None, None)
assert st["name_found"] == 2 and st["ext_found"] == 2 and st["error"] is None, st
assert st["coverage"] == {"porn": {"with": 2, "without": 2}, "mainstream": {"with": 2, "without": 1}}, st["coverage"]
assert not c.execute("SELECT tpdb_performer_id FROM people WHERE id=?", (ids["Gia Name"],)).fetchone()[0]  # no link stored
# resumable: nothing is looked up twice
calls.clear(); cl.post("/api/people/ethnicity_job")
for _ in range(200):
    st = cl.get("/api/people/ethnicity_job").json
    if not st["running"]: break
    time.sleep(0.05)
assert not calls and st["name_total"] == 0 and st["ext_total"] == 0, (calls, st)
# Needs ethnicity queue
q = lambda cat: cl.get(f"/api/people/needs_ethnicity?cat={cat}").json
n = q("porn"); assert [p["name"] for p in n["people"]] == ["Hal Twin", "Ivy Born"] and n["remaining"] == 2, n
m = q("mainstream"); assert [p["name"] for p in m["people"]] == ["Mo None"] and m["people"][0]["photo"], m  # photo first
cl.post(f"/api/people/{ids['Hal Twin']}/eth_skip", json={"skip": True})
n = q("porn"); assert [p["name"] for p in n["people"]] == ["Ivy Born"] and n["skipped"] == 1
r = cl.post(f"/api/person/{ids['Ivy Born']}/edit", json={"ethnicity": "Black"}).json; assert "ethnicity" in r["edited"]
assert E("Ivy Born") == ("Black", "manual") and q("porn")["remaining"] == 0
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Ivy Born"],)).fetchone()
P.apply_tpdb(c, row, {"id": "i1", "extras": {"ethnicity": "Latin"}}); assert E("Ivy Born")[0] == "Black"  # locked
assert cl.post(f"/api/people/{ids['Gia Name']}/eth_undo").status_code == 400  # source value: no undo
assert cl.post(f"/api/people/{ids['Ivy Born']}/eth_undo").json["ok"] and E("Ivy Born") == (None, None)
assert [p["name"] for p in q("porn")["people"]] == ["Ivy Born"]
assert cl.post("/api/people/eth_skips/reset", json={"cat": "porn"}).json["reset"] == 1 and q("porn")["remaining"] == 2
print("ok")
