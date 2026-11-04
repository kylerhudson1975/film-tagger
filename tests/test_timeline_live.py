"""Career timeline on a person page while the ThePornDB scene list is still loading: /insights says loading:true and is
never cached from a part-loaded list (also after a partial save every 5 pages), and the first answer after the load ends
counts every scene (all years). Run: python tests/test_timeline_live.py"""
import os, sys, tempfile, threading, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(1,'Mine','scene','Tess Timeline','2012-03-01',2012)")
c.execute("INSERT INTO files(film_id,path) VALUES(1,'D:\\S\\Mine.mp4')"); c.commit()
N, PER = 650, 100          # 7 pages: a partial save after page 5, the last two pages only have 2023/2024 scenes
gate = threading.Event(); at = {"page": 0}
def tp(path, tok, **k):
    if path == "/performers": return {"data": [{"id": "tt", "name": "Tess Timeline", "extras": {}}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1); at["page"] = p
        if p == 6: gate.wait(20)
        ids = range((p - 1) * PER, min(p * PER, N))
        yr = lambda i: 2023 + (i % 2) if i >= 500 else 2013 + i % 10
        return {"data": [{"id": f"s{i:04d}", "title": f"Scene {i}", "date": f"{yr(i)}-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": 7, "current_page": p}}
    return {"data": {"id": "tt", "name": "Tess Timeline", "extras": {}}}
tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Tess Timeline").json["id"]
c.execute("UPDATE people SET birthday='1990-01-01' WHERE id=?", (pid,)); c.commit()
j = cl.get(f"/api/person/{pid}").json
assert j["scenes_loading"], "scene load should be running"
for _ in range(400):  # wait for the partial save (after page 5) while page 6 is held back
    if c.execute("SELECT tpdb_scenes_json IS NOT NULL FROM people WHERE id=?", (pid,)).fetchone()[0] and at["page"] == 6: break
    time.sleep(0.05)
a = cl.get(f"/api/people/{pid}/insights").json
assert a["loading"] is True and a["cached"] is False, a.get("loading")
b = cl.get(f"/api/people/{pid}/insights").json
assert b["loading"] is True and b["cached"] is False, "a part-loaded answer must never be cached"
years_partial = {y["k"] for y in a["years"]}
assert 2023 not in years_partial and 2024 not in years_partial and a["total"] <= 501, (a["total"], sorted(years_partial))
gate.set()
for _ in range(400):
    if not cl.get(f"/api/person/{pid}").json["scenes_loading"]: break
    time.sleep(0.05)
f = cl.get(f"/api/people/{pid}/insights").json
yrs = {y["k"] for y in f["years"]}
assert f["loading"] is False and f["cached"] is False
assert {2012, 2023, 2024} <= yrs and f["total"] == N + 1, (f["total"], sorted(yrs))
assert f["career"] == [2012, 2024]
g = cl.get(f"/api/people/{pid}/insights").json
assert g["cached"] is True and g["total"] == N + 1   # the complete answer is cached
print("ok")
