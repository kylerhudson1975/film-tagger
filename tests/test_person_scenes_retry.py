"""Person Scenes: a failed ThePornDB page is saved as a partial list with an error and Retry;
the person page does not keep restarting the load (which left 'still loading' up forever)."""
import os, sys, json, tempfile, time, urllib.error
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb

c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(1,'Owned','scene','Casey Star','2020-01-01')")
c.commit()

N, PER = 250, 100
# last_page claims 3; page 3 is never needed if we already have the total, but we serve it too
calls = []
fail_page = {2}  # first attempt: page 2 raises

class Boom(urllib.error.HTTPError):
    def __init__(self, code=503):
        super().__init__("https://api.theporndb.net/x", code, "err", hdrs=None, fp=None)

def tp(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "cs", "name": "Casey Star", "extras": {}, "aliases": []}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        if p in fail_page:
            raise Boom(503)
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
        time.sleep(0.05)
    raise AssertionError("timeout", {k: j.get(k) for k in ("scenes_loading", "scenes_error", "scenes_tpdb_total")} if j else None,
                         "scenes", len(j.get("scenes") or []) if j else None, "calls", calls)

# --- page 2 fails: partial page 1 is kept, loading stops, error is shown, later polls do not refetch ---
j = wait(lambda x: x.get("scenes_error") and not x.get("scenes_loading"))
assert j["scenes_error"] == "ThePornDB HTTP 503", j["scenes_error"]
sc = [x for x in j["scenes"] if not x.get("film_id")]
assert len(sc) == 100, len(sc)
assert j["scenes_tpdb_total"] == N
assert calls[0] == 1 and 2 in calls
n_calls = len(calls)
# several more GETs must not start a new load
for _ in range(8):
    j2 = cl.get(f"/api/person/{pid}").json
    time.sleep(0.05)
assert j2.get("scenes_error") == "ThePornDB HTTP 503" and not j2.get("scenes_loading")
assert len(calls) == n_calls, (calls, n_calls)  # no silent restart

# --- Retry resumes at page 2 (does not refetch page 1) and finishes ---
fail_page.clear()
before = list(calls)
j = cl.get(f"/api/person/{pid}?scenes_retry=1").json
j = wait(lambda x: not x.get("scenes_loading") and not x.get("scenes_error") and len([z for z in (x.get("scenes") or []) if not z.get("film_id")]) == N)
sc = [x for x in j["scenes"] if not x.get("film_id")]
assert len(sc) == N, len(sc)
assert j["scenes_tpdb_total"] == N and j["scenes_error"] is None
new_pages = calls[len(before):]
assert 1 not in new_pages, new_pages  # resumed, did not refetch page 1
assert 2 in new_pages and 3 in new_pages, new_pages

# --- a last_page that is one too high must not request the 401-overshoot page once the total is in hand ---
c.execute("UPDATE people SET tpdb_scenes_json=NULL, tpdb_scenes_at=NULL, tpdb_scenes_total=NULL WHERE id=?", (pid,)); c.commit()
# drop in-memory SCN state by forcing a fresh process state: clear app people module cache
import people as P
# SCN lives in the closures of the registered app; reach it via a get then failed... easier: call with refresh after clearing db
# The in-memory SCN may still have no error (previous load succeeded). Clearing json + refresh starts fresh.
calls.clear()
overshoot = []
def tp2(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "cs", "name": "Casey Star", "extras": {}, "aliases": []}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        if p >= 4:
            overshoot.append(p)
            raise urllib.error.HTTPError("https://api.theporndb.net/x", 401, "Unauthenticated", hdrs=None, fp=None)
        ids = range((p - 1) * PER, min(p * PER, N))
        return {"data": [{"id": f"t{i:04d}", "title": f"Scene {i}", "date": "2020-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": 4, "current_page": p}}  # lies: last_page 4, but 250 scenes is 3 pages
    return {"data": {"id": "cs", "name": "Casey Star", "aliases": [], "extras": {}}}
tpdb.call_retry = tp2
j = cl.get(f"/api/person/{pid}?refresh=1").json
j = wait(lambda x: not x.get("scenes_loading") and (x.get("scenes_error") or len([z for z in (x.get("scenes") or []) if not z.get("film_id")]) == N))
assert not j.get("scenes_error"), j.get("scenes_error")
sc = [x for x in j["scenes"] if not x.get("film_id")]
assert len(sc) == N, len(sc)
assert overshoot == [], overshoot  # have_all stopped before page 4
print("ok")
