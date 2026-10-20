"""Regression (v1.0.31): full TMDB filmography for everyone with a TMDB person; TPDB scenes section for adult performers."""
import os, sys, io, json, tempfile, urllib.request
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
c = db.connect()
db.set_setting(c, "tmdb_key", "k" * 32); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,year,tmdb_id,cast_) VALUES(1,'Owned Movie',2000,100,'Main Actor, Star Perf')")
c.execute("INSERT INTO films(id,title,kind,tpdb_id,cast_) VALUES(2,'Owned Scene','scene','sc1','Star Perf')")
c.execute("INSERT INTO films(id,title,kind,cast_,notes) VALUES(3,'Flagged','scene','Flag Perf','Review by hand: x')"); c.commit()
TM = {"person/7": {"id": 7, "name": "Main Actor", "biography": "TMDB bio", "profile_path": "/p.jpg", "known_for_department": "Acting",
      "combined_credits": {"cast": [{"id": 100, "title": "Owned Movie", "release_date": "2000-01-01", "media_type": "movie"},
                                    {"id": 200, "title": "Not Owned", "release_date": "2005-01-01", "media_type": "movie"}], "crew": [
                                    {"id": 300, "title": "Directed One", "release_date": "2010-01-01", "job": "Director", "department": "Directing", "media_type": "movie"}]}}}
TM["person/8"] = dict(TM["person/7"], id=8, name="Star Perf", biography="TMDB bio 2")
class R(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass
real = urllib.request.urlopen
def fake(req, timeout=None):
    u = req.full_url if hasattr(req, "full_url") else req
    if "movie/100/credits" in u: return R(json.dumps({"cast": [{"id": 7, "name": "Main Actor"}, {"id": 8, "name": "Star Perf"}], "crew": []}).encode())
    for k, v in TM.items():
        if f"/3/{k}?" in u: return R(json.dumps(v).encode())
    if "search/person" in u: return R(b'{"results": []}')
    raise AssertionError(u)
urllib.request.urlopen = fake
perf = {"id": "pf", "name": "Star Perf", "bio": "TPDB bio", "image": "https://i/x.jpg", "extras": {}}
def tp(path, tok, **k):
    if path == "/performers": return {"data": [dict(perf, name=k.get("q"), id="pf" if k.get("q") == "Star Perf" else "ff")]}
    if path.endswith("/scenes"): return {"data": [{"id": "sc1", "title": "Owned Scene", "date": "2024-01-01", "site": {"name": "S"}, "poster": "https://i/s1.jpg"},
                                                  {"id": "sc2", "title": "Other Scene", "date": "2025-01-01", "site": {"name": "S"}, "poster": "https://i/s2.jpg"}]}
    return {"data": dict(perf, id=path.split("/")[-1])}
tpdb.call_retry = tp
cl = app.app.test_client()
import time
def page(name):
    pid = cl.get("/api/person/lookup?name=" + name).json["id"]
    for _ in range(100):  # ThePornDB scene list loads in the background
        j = cl.get(f"/api/person/{pid}").json
        if not j.get("scenes_loading"): return j
        time.sleep(0.05)
    return j
m = page("Main Actor")  # mainstream: full TMDB filmography, owned + not owned
assert m["person"]["source"] == "TMDB" and m["total"] == 3 and m["owned"] == 1, m
assert {g["dept"] for g in m["groups"]} == {"Acting", "Directing"} and not m["scenes"]
s = page("Star Perf")  # adult performer on TPDB: TPDB bio, TMDB filmography kept, Scenes section
assert s["person"]["bio"] == "TPDB bio" and s["person"]["source"].startswith("ThePornDB") and s["total"] == 3, s
assert [(x["title"], bool(x["film_id"]), bool(x["thumb"])) for x in s["scenes"]] == [("Owned Scene", True, True), ("Other Scene", False, True)]
f = page("Flag Perf")  # review-flagged: scenes without images, no photo
assert f["scenes"] and all(not x["thumb"] and not x["poster"] for x in f["scenes"]) and not f["person"]["photo"]
urllib.request.urlopen = real
print("ok")
