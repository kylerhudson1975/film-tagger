"""Ethnicity: column migration, normalisation, Edit profile field (a protected hand edit), add-only fill from ThePornDB,
People search matching ethnicity / nationality plus the eth= filter, per-value counts (/api/people/ethnicities) and the
backfill job (/api/people/ethnicity_job: Wikidata P172 then ThePornDB, resumable). Run: python tests/test_ethnicity.py"""
import os, sys, json, time, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app, tpdb, birthdates, people as P
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tpdb_key", "t")
# migration
cols = {r[1] for r in c.execute("PRAGMA table_info(people)")}
assert {"ethnicity", "ethnicity_source", "ethnicity_checked_at"} <= cols, cols
# normalisation
N = P.norm_ethnicity
assert N("Caucasian") == N("white") == "White" and N("Latin") == N("Latina") == N("latino") == N("Hispanic") == "Latina/Latino"
assert N("African American") == "Black" and N("Indian") == "South Asian" and N("Asian") == "Asian"
assert N("Other") is None and N("") is None and N("Irish Americans") == "White" and N("Armenians") == "Armenians" and N("Armenians", keep=False) is None
assert "Jewish" in P.ETHNICITIES and P.ETHNICITIES.index("Jewish") == P.ETHNICITIES.index("Black") + 1
assert N("Jewish people") == N("Ashkenazi Jews") == N("ashkenazi") == N("Sephardi") == N("Jewish Americans") == "Jewish"
assert N("jew") == N("Jews") == N("Israeli Jews") == N("Russian Jews") == N("Polish Jews") == "Jewish"
names = ["Ana Lopez", "Bea White", "Cy Hand", "Di Wiki", "Ed None", "Flo Ruiz"]
for i, n in enumerate(names, 1):
    c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(?,?,?,?)", (i, f"S{i}", "scene", n))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\S{i}.mp4"))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in names}
# ThePornDB fill is add-only and normalised, with its source recorded
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Ana Lopez"],)).fetchone()
P.apply_tpdb(c, row, {"id": "tpA", "extras": {"ethnicity": "Latin", "nationality": "Colombian"}})
r = c.execute("SELECT ethnicity, ethnicity_source FROM people WHERE id=?", (ids["Ana Lopez"],)).fetchone()
assert tuple(r) == ("Latina/Latino", "theporndb tpA"), tuple(r)
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Ana Lopez"],)).fetchone()
P.apply_tpdb(c, row, {"id": "tpA", "extras": {"ethnicity": "Caucasian"}})
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Ana Lopez"],)).fetchone()[0] == "Latina/Latino"
# hand edit: stored normalised, source 'hand edit', locked against automatic fills
e = cl.post(f"/api/person/{ids['Cy Hand']}/edit", json={"ethnicity": "caucasian"}).json
assert e["ok"] and "ethnicity" in e["edited"]
pp = cl.get(f"/api/person/{ids['Cy Hand']}").json["person"]
assert pp["ethnicity"] == "White" and pp["ethnicity_source"] == "manual", pp
cl.post(f"/api/person/{ids['Cy Hand']}/edit", json={"ethnicity": ""})  # cleared by hand: stays empty and locked
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Cy Hand"],)).fetchone()
P.apply_tpdb(c, row, {"id": "tpC", "extras": {"ethnicity": "Asian"}})
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Cy Hand"],)).fetchone()[0] is None
cl.post(f"/api/person/{ids['Cy Hand']}/edit", json={"ethnicity": "White"})
# backfill: Wikidata P172 for a TMDB-linked person, ThePornDB for a linked performer; hand edit / filled untouched
c.execute("UPDATE people SET tmdb_person_id=501 WHERE id=?", (ids["Di Wiki"],))
c.execute("UPDATE people SET tpdb_performer_id='tpB' WHERE id=?", (ids["Bea White"],))
c.execute("UPDATE people SET tpdb_performer_id='tpE' WHERE id=?", (ids["Ed None"],))
c.execute("UPDATE people SET tpdb_performer_id='tpX', tmdb_person_id=502 WHERE id=?", (ids["Cy Hand"],))
c.execute("UPDATE people SET nationality='Mexican' WHERE id=?", (ids["Flo Ruiz"],)); c.commit()
sparql = []
def fake_sparql(q):
    sparql.append(q)
    assert '"502"' not in q, "hand-edited person must not be looked up"
    b = [{"k": {"value": "501"}, "p": {"value": "http://www.wikidata.org/entity/Q9"}, "e": {"value": "x"}, "eLabel": {"value": "African Americans"}}] if '"501"' in q else []
    return {"results": {"bindings": b}}
birthdates._sparql = fake_sparql
tp_calls = []
def fake_tpdb(path, tok, **k):
    tp_calls.append(path)
    if path == "/performers": return {"data": []}  # name search (Flo Ruiz, never linked): nothing found
    eth = {"tpB": "Caucasian", "tpE": "Other"}[path.rsplit("/", 1)[1]]
    return {"data": {"id": "x", "extras": {"ethnicity": eth}}}
tpdb.call_retry = fake_tpdb
def run():
    cl.post("/api/people/ethnicity_job")
    for _ in range(200):
        st = cl.get("/api/people/ethnicity_job").json
        if not st["running"]: return st
        time.sleep(0.05)
st = run()
eth = {n: c.execute("SELECT ethnicity FROM people WHERE id=?", (ids[n],)).fetchone()[0] for n in names}
assert eth == {"Ana Lopez": "Latina/Latino", "Bea White": "White", "Cy Hand": "White", "Di Wiki": "Black", "Ed None": None, "Flo Ruiz": None}, eth
assert c.execute("SELECT ethnicity_source FROM people WHERE id=?", (ids["Di Wiki"],)).fetchone()[0] == "wikidata Q9"
assert st["wikidata"] == 1 and st["tpdb"] == 1 and st["error"] is None, st
assert sorted(tp_calls) == ["/performers", "/performers/tpB", "/performers/tpE"], tp_calls
# resumable: a second run looks nobody up again (Ed None was checked, nothing published)
tp_calls.clear(); sparql.clear(); st = run()
assert st["total"] == 0 and not tp_calls and not sparql, (st, tp_calls)
# search: an ethnicity word (any variant) finds people with that ethnicity; nationality words too; names still work
S = lambda **a: sorted(p["name"] for p in cl.get("/api/people/search", query_string=a).json["people"])
assert S(q="Latina") == S(q="hispanic") == ["Ana Lopez"], S(q="Latina")
assert S(q="white") == ["Bea White", "Cy Hand"] and S(q="caucasian") == ["Bea White", "Cy Hand"]
assert S(q="black") == ["Di Wiki"] and S(q="mexican") == ["Flo Ruiz"] and S(q="ana") == ["Ana Lopez"]
assert S(q="asian") == []  # 'asian' is not a substring hit on Caucasian / White
assert S(eth="White") == ["Bea White", "Cy Hand"] and S(q="cy", eth="White") == ["Cy Hand"] and S(q="ana", eth="White") == []
# counts per value
v = cl.get("/api/people/ethnicities").json
assert {x["value"]: x["n"] for x in v["values"]} == {"White": 2, "Latina/Latino": 1, "Black": 1}, v
assert v["unknown"] == 2 and v["people_with"] == 4 and v["sources"] == {"theporndb": 2, "manual": 1, "wikidata": 1}, v
# several values: a hand edit can keep a still-unmapped label plus a main group; each counts
cl.post(f"/api/person/{ids['Flo Ruiz']}/edit", json={"ethnicity": "Armenians, white"})
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Flo Ruiz"],)).fetchone()[0] == "Armenians, White"
assert "Flo Ruiz" in S(q="white") and "Flo Ruiz" in S(eth="White") and S(eth="Armenians") == ["Flo Ruiz"] and S(q="armenian") == ["Flo Ruiz"]
v = {x["value"]: x["n"] for x in cl.get("/api/people/ethnicities").json["values"]}
assert v["White"] == 3 and v["Armenians"] == 1, v
# ancestry fold: two main groups become a Needs ethnicity conflict; a hand edit is never touched
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=? WHERE id=?", ("Jewish people, White", "wikidata Qx", ids["Ed None"]))
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=?, edited_fields=? WHERE id=?",
          ("Ashkenazi Jews", "manual", json.dumps(["ethnicity"]), ids["Flo Ruiz"]))
c.commit()
folded, conflict = P.apply_ancestry_fold(c)
assert conflict >= 1
ed = c.execute("SELECT ethnicity, ethnicity_listings, ethnicity_source FROM people WHERE id=?", (ids["Ed None"],)).fetchone()
assert ed["ethnicity"] is None and json.loads(ed["ethnicity_listings"]) == ["Jewish people", "White"] and ed["ethnicity_source"] == "wikidata Qx", tuple(ed)
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Flo Ruiz"],)).fetchone()[0] == "Ashkenazi Jews"  # manual locked
neth = cl.get("/api/people/needs_ethnicity?cat=porn").json
ed_hit = [p for p in neth["people"] if p["id"] == ids["Ed None"]]
assert ed_hit and ed_hit[0]["conflict"] and ed_hit[0]["listings"] == ["Jewish people", "White"], ed_hit
# tagging a conflict is a locked hand edit; automatic fill cannot overwrite it
assert cl.post(f"/api/person/{ids['Ed None']}/edit", json={"ethnicity": "White"}).json["ok"]
assert tuple(c.execute("SELECT ethnicity, ethnicity_source FROM people WHERE id=?", (ids["Ed None"],)).fetchone()) == ("White", "manual")
assert ids["Ed None"] not in [p["id"] for p in cl.get("/api/people/needs_ethnicity?cat=porn").json["people"]]
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Ed None"],)).fetchone()
P.apply_tpdb(c, row, {"id": "tpE2", "extras": {"ethnicity": "Asian"}})
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Ed None"],)).fetchone()[0] == "White"
# summary panel carries it
assert cl.get(f"/api/people/{ids['Di Wiki']}/summary").json["ethnicity"] == "Black"
print("ok")
