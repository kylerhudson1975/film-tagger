"""First open of a person page must return immediately even when ThePornDB is slow.
The lookup runs in the background; the page shows a loading status and the list arrives on later polls."""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb, people
people.TPDB_PAGE_SLEEP = False
c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(1,'Owned','scene','Slow First','2020-01-01')")
c.commit()

def tp(path, tok, **k):
    # Performer lookup is slow; a blocking GET would wait this long.
    if path == "/performers" or (path.startswith("/performers/") and not path.endswith("/scenes") and not path.endswith("/movies")):
        time.sleep(3)
        if path == "/performers":
            return {"data": [{"id": "sf", "name": "Slow First", "extras": {}, "aliases": []}]}
        return {"data": {"id": "sf", "name": "Slow First", "aliases": [], "extras": {}}}
    if path.endswith("/movies"):
        return {"data": [], "meta": {"total": 0, "last_page": 0, "current_page": 1}}
    if path.endswith("/scenes"):
        p = k.get("page", 1)
        if p > 1:
            return {"data": [], "meta": {"total": 2, "last_page": 1, "current_page": p}}
        return {"data": [{"id": "a", "title": "A", "date": "2020-01-01", "site": {"name": "S"}},
                         {"id": "b", "title": "B", "date": "2020-02-01", "site": {"name": "S"}}],
                "meta": {"total": 2, "last_page": 1, "current_page": 1}}
    time.sleep(3)
    return {"data": {"id": "sf", "name": "Slow First", "aliases": [], "extras": {}}}

tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Slow First").json["id"]
t0 = time.time()
j = cl.get(f"/api/person/{pid}").json
elapsed = time.time() - t0
assert elapsed < 1.0, f"first GET blocked for {elapsed:.2f}s"
assert j.get("scenes_loading"), j
got = None
for _ in range(80):
    got = cl.get(f"/api/person/{pid}").json
    sc = [x for x in (got.get("scenes") or []) if not x.get("film_id")]
    if not got.get("scenes_loading") and len(sc) == 2 and not got.get("scenes_error"):
        break
    time.sleep(0.25)
else:
    raise AssertionError("scenes never arrived", {k: got.get(k) for k in ("scenes_loading","scenes_error","scenes_tpdb_total")},
                         "n", len(got.get("scenes") or []) if got else None)
print("ok", "first_get_sec", round(elapsed, 3))
