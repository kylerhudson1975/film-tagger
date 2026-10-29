"""Queued films' cast get birth-date lookups first: POST /api/queue starts them (ahead of the background job, which waits and
then skips them), confident results saved, ambiguous / not found go to the review list (queued first, with links), hand
edits never looked up or overwritten, export_queue lists who still lacks a birth date, 'look up now' retries. No network.
Run: python tests/test_queue_birthdates.py"""
import os, sys, json, time, tempfile
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, birthdates as bd, bdsources
c = db.connect(); bd.ensure(c)
ids = []
for t, cast in [("Queued One", "Pat Test, Has Date, Hand Cleared"), ("Queued Two", "Amb Test, Nf Test"), ("Other", "Bg One")]:
    ids.append(c.execute("INSERT INTO films(title,year,cast_,primary_genre) VALUES(?,1970,?,'Drama')", (t, cast)).lastrowid)
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Has Date','has date','1940-01-01')")
c.execute("INSERT INTO people(name,name_norm,edited_fields) VALUES('Hand Cleared','hand cleared','[\"birthday\"]')"); c.commit()
calls = []
def fake(name, *a, **k):
    calls.append(name)
    if name == "Pat Test": return dict(status="filled", birthday="1952-05-01", source="wikidata Q9", detail="test")
    if name == "Amb Test": return dict(status="ambiguous", detail=json.dumps([{"qid": "Q1", "birthday": "1950-01-01"}, {"qid": "Q2", "birthday": "1960-01-01"}]))
    if name == "Bg One": time.sleep(0.2)
    return dict(status="not_found", detail="none")
bd.lookup = fake
bdsources.lookup = lambda *a, **k: dict(hit=None, cands=[], tried=["Boobpedia"], skipped={})
cl = app.app.test_client()
# offline (temp DB): queueing never goes online
assert cl.post("/api/queue", json={"film_ids": [ids[0]]}).json["birthdate_lookups"] == 0 and not calls
cl.delete("/api/queue", json={})
bd.enabled = lambda: True
# background job waits while queued films are looked up
bd.PRIO["running"] = True
assert bd.run_job(db.connect); time.sleep(1.5)
assert bd.JOB["current"].startswith("(waiting") and not calls, (bd.JOB, calls)
bd.PRIO["running"] = False
r = cl.post("/api/queue", json={"film_ids": [ids[0], ids[1]]}).json
assert r["count"] == 2 and r["birthdate_lookups"] == 3, r
for _ in range(100):
    if not bd.PRIO["running"] and not bd.JOB["running"]: break
    time.sleep(0.1)
assert calls[:3] == ["Pat Test", "Amb Test", "Nf Test"], calls  # queued cast first
assert calls.count("Pat Test") == 1 and "Bg One" in calls and "Hand Cleared" not in calls, calls  # job skipped them, did the rest
assert c.execute("SELECT birthday, birth_date_source FROM people WHERE name_norm='pat test'").fetchone()[:] == ("1952-05-01", "wikidata Q9")
assert c.execute("SELECT birthday FROM people WHERE name_norm='hand cleared'").fetchone()[0] is None
assert c.execute("SELECT COUNT(*) FROM birthdate_lookup WHERE name_norm='hand cleared'").fetchone()[0] == 0
st = cl.get("/api/birthdates/status").json["queued"]; assert st["done"] == 3 and st["filled"] == 1 and st["review"] == 2, st
assert [x["name"] for x in st["results"] if x["birthday"]] == ["Pat Test"]
rv = cl.get("/api/birthdates/review").json
assert rv["queued_count"] == 2 and [x["name"] for x in rv["review"]] == ["Amb Test", "Nf Test"], rv
nf = rv["review"][1]; assert nf["queued"] == ["Queued Two (1970)"] and nf["status"] == "not_found" and "iafd.com" in nf["iafd_search"]
assert [l["label"] for l in nf["links"]] == ["Wikipedia", "Boobpedia", "FreeOnes", "Babepedia"], nf
assert not c.execute("SELECT birthday FROM people WHERE name_norm='amb test'").fetchone()  # ambiguous: not saved
ex = {f["title"]: f["cast_missing_birthdate"] for f in db.export_queue(c)}
assert ex == {"Queued One": [{"name": "Hand Cleared", "status": "hand_edited"}],
              "Queued Two": [{"name": "Amb Test", "status": "ambiguous"}, {"name": "Nf Test", "status": "not_found"}]}, ex
# queueing again: nothing new to do; 'look up now' retries the not found one (not the one in review)
assert cl.post("/api/queue", json={"film_ids": [ids[1]]}).json["birthdate_lookups"] == 0
calls.clear(); assert cl.post("/api/birthdates/queue-lookup", json={}).json["added"] == 1
for _ in range(50):
    if not bd.PRIO["running"]: break
    time.sleep(0.1)
assert calls == ["Nf Test"], calls
cl.delete("/api/queue", json={}); assert cl.get("/api/birthdates/review").json["queued_count"] == 0
print("ok")
