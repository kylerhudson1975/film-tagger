"""A truncated ThePornDB response (IncompleteRead) is retried; a lasting stall keeps the partial list and Retry."""
import os, sys, json, tempfile, time, http.client
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb, people
people.TPDB_PAGE_SLEEP = False
time.sleep = lambda s: None  # skip backoff so the retry loop is testable

c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(1,'Owned','scene','Casey Star','2020-01-01')")
c.commit()

N, PER = 250, 100
calls = []
# page 2 fails twice (IncompleteRead), then succeeds
left = {2: 2}

def tp(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "cs", "name": "Casey Star", "extras": {}, "aliases": []}]}
    if path.endswith("/movies"):
        return {"data": [], "meta": {"total": 0, "last_page": 0, "current_page": 1}}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        if left.get(p, 0) > 0:
            left[p] -= 1
            raise http.client.IncompleteRead(b"partial")
        ids = range((p - 1) * PER, min(p * PER, N))
        return {"data": [{"id": f"s{i:04d}", "title": f"Scene {i}", "date": "2020-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": (N + PER - 1) // PER, "current_page": p}}
    return {"data": {"id": "cs", "name": "Casey Star", "aliases": [], "extras": {}}}

tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Casey Star").json["id"]

def wait(pred, loops=200):
    j = None
    for _ in range(loops):
        j = cl.get(f"/api/person/{pid}").json
        if pred(j): return j
    raise AssertionError("timeout", {k: j.get(k) for k in ("scenes_loading", "scenes_error", "scenes_tpdb_total")} if j else None,
                         "scenes", len(j.get("scenes") or []) if j else None, "calls", calls)

j = wait(lambda x: not x.get("scenes_loading") and not x.get("scenes_error") and len([z for z in (x.get("scenes") or []) if not z.get("film_id")]) == N)
assert j["scenes_error"] is None
sc = [x for x in j["scenes"] if not x.get("film_id")]
assert len(sc) == N, len(sc)
# page 2 was attempted more than once, then 3 finished
assert calls.count(2) >= 3, calls
assert calls.count(1) == 1 and 3 in calls, calls

# lasting IncompleteRead: partial list + error, Retry resumes
c.execute("UPDATE people SET tpdb_scenes_json=NULL, tpdb_scenes_at=NULL, tpdb_scenes_total=NULL WHERE id=?", (pid,)); c.commit()
calls.clear()
def boom(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "cs", "name": "Casey Star", "extras": {}, "aliases": []}]}
    if path.endswith("/movies"):
        return {"data": [], "meta": {"total": 0, "last_page": 0, "current_page": 1}}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        if p == 2:
            raise http.client.IncompleteRead(b"partial")
        ids = range((p - 1) * PER, min(p * PER, N))
        return {"data": [{"id": f"t{i:04d}", "title": f"Scene {i}", "date": "2020-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": (N + PER - 1) // PER, "current_page": p}}
    return {"data": {"id": "cs", "name": "Casey Star", "aliases": [], "extras": {}}}
tpdb.call_retry = boom
j = cl.get(f"/api/person/{pid}?refresh=1").json
j = wait(lambda x: x.get("scenes_error") and not x.get("scenes_loading"))
assert j["scenes_error"] == "IncompleteRead", j["scenes_error"]
sc = [x for x in j["scenes"] if not x.get("film_id")]
assert len(sc) == 100, len(sc)
n_calls = len(calls)
for _ in range(6):
    j2 = cl.get(f"/api/person/{pid}").json
assert j2.get("scenes_error") == "IncompleteRead" and not j2.get("scenes_loading")
assert len(calls) == n_calls, (calls, n_calls)

tpdb.call_retry = tp
left.clear()
before = list(calls)
j = cl.get(f"/api/person/{pid}?scenes_retry=1").json
j = wait(lambda x: not x.get("scenes_loading") and not x.get("scenes_error") and len([z for z in (x.get("scenes") or []) if not z.get("film_id")]) == N)
assert 1 not in calls[len(before):], calls[len(before):]
assert len([z for z in j["scenes"] if not z.get("film_id")]) == N
print("ok")
