"""The person page lists ThePornDB movies as well as scenes, labeled, with movie URLs."""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb, people
people.TPDB_PAGE_SLEEP = False
c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(1,'Owned Feature','film','Sharon Test','1985-06-01',1985)")
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(2,'A Scene','scene','Sharon Test','2001-01-01')")
c.commit()
def tp(path, tok, **k):
    if path == "/performers":
        return {"data": [{"id": "sk", "name": "Sharon Test", "extras": {}, "aliases": []}]}
    if path.endswith("/movies"):
        data = [{"id": f"m{i}", "title": t, "date": "1985-06-01", "site": {"name": "Classic Studio"}, "type": "Movie", "slug": f"m{i}-slug"}
                for i, t in enumerate(("Owned Feature", "Other Feature", "Third Feature"), 1)]
        return {"data": data, "meta": {"total": 3, "last_page": 1, "current_page": 1}}
    if path.endswith("/scenes"):
        return {"data": [{"id": "s1", "title": "Later Scene", "date": "2001-01-01", "site": {"name": "S"}, "slug": "s1-slug"},
                         {"id": "s2", "title": "Other Scene", "date": "2002-01-01", "site": {"name": "S"}, "slug": "s2-slug"}],
                "meta": {"total": 2, "last_page": 1, "current_page": 1}}
    return {"data": {"id": "sk", "name": "Sharon Test", "aliases": [], "extras": {}}}
tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Sharon Test").json["id"]
j = None
for _ in range(200):
    j = cl.get(f"/api/person/{pid}").json
    if not j.get("scenes_loading") and not j.get("scenes_error") and j.get("scenes") is not None:
        tpdb_n = [x for x in j["scenes"]]
        if j.get("scenes_tpdb_total") == 5:
            break
    time.sleep(0.05)
else:
    raise AssertionError("movies+scenes did not load", {k: j.get(k) for k in ("scenes_loading","scenes_error","scenes_tpdb_total","scenes_total","scenes_owned")},
                         "titles", [x.get("title") for x in (j.get("scenes") or [])])
titles = {x["title"]: x for x in j["scenes"]}
assert "Later Scene" in titles and titles["Later Scene"]["kind"] == "scene"
assert titles["Later Scene"]["url"].endswith("/scenes/s1-slug")
assert "Other Feature" in titles and titles["Other Feature"]["kind"] == "movie"
assert titles["Other Feature"]["url"].endswith("/movies/m2-slug")
assert titles["Owned Feature"]["film_id"] == 1  # matched the library classic by title+year
assert j["scenes_owned"] >= 1 and j["scenes_tpdb_total"] == 5
# cached complete: opening again does not drop movies
j2 = cl.get(f"/api/person/{pid}").json
assert not j2.get("scenes_loading") and j2["scenes_tpdb_total"] == 5
assert sum(1 for x in j2["scenes"] if x.get("kind")=="movie") >= 2
print("ok movies", "owned", j["scenes_owned"], "total", j["scenes_tpdb_total"])
