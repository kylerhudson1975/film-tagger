"""Birth dates from Wikidata/Wikipedia: confident results saved with a source, hand edits / existing dates never
overwritten, ThePornDB/TMDB refreshes without a date keep the found one, queue order, review 'none'. No network.
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
print("ok")
