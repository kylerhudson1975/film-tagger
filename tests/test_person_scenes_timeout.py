"""A single ThePornDB page timeout is retried and does not end the load; later pages still arrive."""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb, people
people.TPDB_PAGE_SLEEP = False
people.TPDB_PAGE_TRIES = 3
people.TPDB_PAGE_ROUNDS = 3
c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(1,'Owned','scene','Tessa Timeout','2020-01-01')")
c.commit()
N, PER = 250, 100
calls = []
fails = {2: 2}  # page 2 times out twice, then succeeds

def tp(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "tt", "name": "Tessa Timeout", "extras": {}, "aliases": []}]}
    if path.endswith("/movies"):
        return {"data": [], "meta": {"total": 0, "last_page": 0, "current_page": 1}}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        left = fails.get(p, 0)
        if left:
            fails[p] = left - 1
            raise TimeoutError("timed out")
        ids = range((p - 1) * PER, min(p * PER, N))
        return {"data": [{"id": f"s{i:04d}", "title": f"Scene {i}", "date": "2020-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": (N + PER - 1) // PER, "current_page": p}}
    return {"data": {"id": "tt", "name": "Tessa Timeout", "aliases": [], "extras": {}}}

tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Tessa Timeout").json["id"]
j = None
for _ in range(200):
    j = cl.get(f"/api/person/{pid}").json
    sc = [x for x in (j.get("scenes") or []) if not x.get("film_id")]
    if not j.get("scenes_loading") and not j.get("scenes_error") and len(sc) == N:
        break
    time.sleep(0.05)
else:
    raise AssertionError("did not finish", {k: j.get(k) for k in ("scenes_loading","scenes_error","scenes_tpdb_total")},
                         "n", len(j.get("scenes") or []), "calls", calls)
assert j.get("scenes_error") is None, j.get("scenes_error")
assert j["scenes_tpdb_total"] == N
assert calls.count(2) >= 3  # failed twice, then succeeded
print("ok timeout-retried", "calls", calls)
