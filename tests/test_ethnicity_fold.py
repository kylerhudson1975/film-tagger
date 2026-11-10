"""Ancestry fold: Wikidata P172 heritage labels fold into a main Ethnicity group; nationality-style and
undecided labels stay as published; two or more main groups go to Needs ethnicity with listings; a
quick-tag is a locked hand edit. Horn of Africa values map to Black. Run: python tests/test_ethnicity_fold.py"""
import os, sys, json, time, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app, birthdates, people as P

N, R = P.norm_ethnicity, P.resolve_ethnicity
# approved heritage mappings
assert N("Irish Americans") == N("Italian Americans") == N("Lemkos") == "White"
assert N("Māori") == N("Maori") == "Pacific Islander"
assert N("Taíno people") == N("Taino") == N("Cherokee") == "Native American"
assert N("Korean Americans") == N("Minangkabau") == N("British Chinese") == "Asian"
assert N("Indian Americans") == N("Punjabis") == "South Asian"
assert N("Cuban Americans") == N("Stateside Puerto Ricans") == N("Mexican Americans") == "Latina/Latino"
assert N("Lebanese Americans") == N("Palestinians") == "Middle Eastern"
assert N("African Brazilians") == N("Somali") == N("Ethiopian Americans") == N("Eritrean") == N("Nubian") == "Black"
assert N("Amhara") == N("Oromo") == N("Habesha") == "Black"
assert N("Eurasian") == "Mixed"
# left as published (nationality-style or still undecided)
for raw in ("Armenians", "Spanish Americans", "Brazilian Americans", "Galicians", "Russians",
            "Bahamian Americans", "Puerto Ricans", "Berbers", "Métis", "Metis", "Louisiana Creole people",
            "Scandinavians", "French people", "Americans", "Irish people", "English people", "British Americans",
            "Kenyan"):
    assert N(raw) == raw, (raw, N(raw))
# resolve: one main group wins, even with leftover unmapped labels
assert R(["Irish Americans", "English people"]) == ("White", False, ["Irish Americans", "English people"])
assert R(["Jewish people", "Russians"]) == ("Jewish", False, ["Jewish people", "Russians"])
assert R(["African Brazilians"]) == ("Black", False, ["African Brazilians"])
assert R(["Eurasian"]) == ("Mixed", False, ["Eurasian"])
assert R(["Caucasian", "Eurasian"]) == ("White", False, ["Caucasian", "Eurasian"])  # Mixed does not compete
# two or more decisive groups: conflict, no auto-assignment
assert R(["Ashkenazi Jews", "Irish Americans"])[1] is True
assert R(["African Americans", "Māori"]) == (None, True, ["African Americans", "Māori"])
assert R(["English people", "Irish people"]) == ("English people, Irish people", False, ["English people", "Irish people"])

c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tpdb_key", "t"); c.commit()
# One mainstream film (several credits) and one adult scene, so Needs ethnicity can be checked on both sides
c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(1,'M1',NULL,'Pat Fold, Quinn Conflict, Remy Locked, Sid Raw')")
c.execute("INSERT INTO files(film_id,path) VALUES(1, ?)", (r"D:\M\M1.mkv",))
c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(2,'S1','scene','Terry Twin')")
c.execute("INSERT INTO files(film_id,path) VALUES(2, ?)", (r"D:\S\S1.mp4",))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in
       ("Pat Fold", "Quinn Conflict", "Remy Locked", "Sid Raw", "Terry Twin")}
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=? WHERE id=?",
          ("Irish Americans, English people", "wikidata Q1", ids["Pat Fold"]))
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=? WHERE id=?",
          ("Ashkenazi Jews, Irish Americans", "wikidata Q2", ids["Quinn Conflict"]))
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=?, edited_fields=? WHERE id=?",
          ("Irish Americans", "manual", json.dumps(["ethnicity"]), ids["Remy Locked"]))
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=? WHERE id=?",
          ("Armenians", "wikidata Q3", ids["Sid Raw"]))
c.commit()
folded, conflict = P.apply_ancestry_fold(c)
assert folded == 1 and conflict == 1, (folded, conflict)
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Pat Fold"],)).fetchone()[0] == "White"
q = c.execute("SELECT ethnicity, ethnicity_listings, ethnicity_source FROM people WHERE id=?", (ids["Quinn Conflict"],)).fetchone()
assert q[0] is None and json.loads(q[1]) == ["Ashkenazi Jews", "Irish Americans"] and q[2] == "wikidata Q2", tuple(q)
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Remy Locked"],)).fetchone()[0] == "Irish Americans"
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Sid Raw"],)).fetchone()[0] == "Armenians"
# idempotent
assert P.apply_ancestry_fold(c) == (0, 0)

# Needs ethnicity (mainstream): conflict first, listings present; raw-only Armenians are not in the queue
n = cl.get("/api/people/needs_ethnicity?cat=mainstream").json
names = [p["name"] for p in n["people"]]
assert "Quinn Conflict" in names and "Sid Raw" not in names and "Remy Locked" not in names and "Pat Fold" not in names, n
hit = next(p for p in n["people"] if p["name"] == "Quinn Conflict")
assert hit["conflict"] and hit["listings"] == ["Ashkenazi Jews", "Irish Americans"], hit
assert n["people"][0]["name"] == "Quinn Conflict"  # conflicts sort first
# a tag is a locked hand edit
assert cl.post(f"/api/person/{ids['Quinn Conflict']}/edit", json={"ethnicity": "White"}).json["ok"]
assert tuple(c.execute("SELECT ethnicity, ethnicity_source FROM people WHERE id=?", (ids["Quinn Conflict"],)).fetchone()) == ("White", "manual")
assert "Quinn Conflict" not in [p["name"] for p in cl.get("/api/people/needs_ethnicity?cat=mainstream").json["people"]]
# future fill / another fold must not overwrite the hand tag (even if listings still say Jewish + Irish)
import people as P2
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Quinn Conflict"],)).fetchone()
# ThePornDB add-only
P.apply_tpdb(c, row, {"id": "tpZ", "extras": {"ethnicity": "Asian"}})
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Quinn Conflict"],)).fetchone()[0] == "White"
assert P.apply_ancestry_fold(c) == (0, 0)
assert c.execute("SELECT ethnicity FROM people WHERE id=?", (ids["Quinn Conflict"],)).fetchone()[0] == "White"

# Future Wikidata fill uses the same fold (Irish Americans -> White; Jewish + Irish -> conflict in the queue)
c.execute("UPDATE people SET tmdb_person_id=? WHERE id=?", (801, ids["Terry Twin"]))
c.commit()
def fake_sparql(q):
    if '"801"' in q:
        return {"results": {"bindings": [
            {"k": {"value": "801"}, "p": {"value": "http://www.wikidata.org/entity/Q80"}, "e": {"value": "x"},
             "eLabel": {"value": "Irish Americans"}},
        ]}}
    return {"results": {"bindings": []}}
birthdates._sparql = fake_sparql
cl.post("/api/people/ethnicity_job")
for _ in range(200):
    st = cl.get("/api/people/ethnicity_job").json
    if not st["running"]: break
    time.sleep(0.05)
assert c.execute("SELECT ethnicity, ethnicity_source FROM people WHERE id=?", (ids["Terry Twin"],)).fetchone()[:] == ("White", "wikidata Q80")

# a later fill of a blank person with two main groups goes to the queue
c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(3,'S2','scene','Uma Two')")
c.execute("INSERT INTO files(film_id,path) VALUES(3, ?)", (r"D:\S\S2.mp4",))
c.commit()
uid = cl.get("/api/person/lookup?name=Uma Two").json["id"]
c.execute("UPDATE people SET tmdb_person_id=?, ethnicity_checked_at=NULL WHERE id=?", (802, uid)); c.commit()
def fake_sparql2(q):
    if '"802"' in q:
        return {"results": {"bindings": [
            {"k": {"value": "802"}, "p": {"value": "http://www.wikidata.org/entity/Q82"}, "e": {"value": "x"},
             "eLabel": {"value": "Ashkenazi Jews"}},
            {"k": {"value": "802"}, "p": {"value": "http://www.wikidata.org/entity/Q82"}, "e": {"value": "y"},
             "eLabel": {"value": "Irish Americans"}},
        ]}}
    return {"results": {"bindings": []}}
birthdates._sparql = fake_sparql2
cl.post("/api/people/ethnicity_job")
for _ in range(200):
    st = cl.get("/api/people/ethnicity_job").json
    if not st["running"]: break
    time.sleep(0.05)
r = c.execute("SELECT ethnicity, ethnicity_listings, ethnicity_source FROM people WHERE id=?", (uid,)).fetchone()
assert r[0] is None and set(json.loads(r[1])) == {"Ashkenazi Jews", "Irish Americans"} and r[2] == "wikidata Q82", tuple(r)
neth = cl.get("/api/people/needs_ethnicity?cat=porn").json
hit = next(p for p in neth["people"] if p["id"] == uid)
assert hit["conflict"] and set(hit["listings"]) == {"Ashkenazi Jews", "Irish Americans"}, hit
print("ok")
