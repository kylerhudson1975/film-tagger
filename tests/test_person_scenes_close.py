"""If the last expected page is in and only a handful of ids are missing (duplicates), the load finishes
instead of hanging on a page past last_page."""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb

c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(1,'Owned','scene','Casey Star','2020-01-01')")
c.commit()

N, PER = 250, 100
calls = []
def tp(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "cs", "name": "Casey Star", "extras": {}, "aliases": []}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        if p >= 4:
            raise AssertionError("should not fetch page %s" % p)
        ids = list(range((p - 1) * PER, min(p * PER, N)))
        # two ids on page 3 were already on page 1, so the unique total is 248 of the reported 250
        if p == 3:
            ids = [0, 1] + ids[2:]
        return {"data": [{"id": f"s{i:04d}", "title": f"Scene {i}", "date": "2020-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": 3, "current_page": p}}
    return {"data": {"id": "cs", "name": "Casey Star", "aliases": [], "extras": {}}}

tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Casey Star").json["id"]
j = None
for _ in range(200):
    j = cl.get(f"/api/person/{pid}").json
    if not j.get("scenes_loading"):
        break
    time.sleep(0.05)
else:
    raise AssertionError("timeout", j.get("scenes_loading"), j.get("scenes_error"), calls)
assert not j.get("scenes_error"), j.get("scenes_error")
assert calls == [1, 2, 3], calls
sc = [x for x in j["scenes"] if not x.get("film_id")]
assert len(sc) == 248, len(sc)
assert j["scenes_tpdb_total"] == N
# later polls must not start another load
n = len(calls)
for _ in range(6):
    j2 = cl.get(f"/api/person/{pid}").json
assert not j2.get("scenes_loading") and not j2.get("scenes_error")
assert len(calls) == n
print("ok")
