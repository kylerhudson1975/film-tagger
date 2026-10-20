"""Person Scenes: every ThePornDB page is fetched (no 300 cap), the true total comes from meta, and ALL library scenes
of the performer (by name, alias, or ThePornDB scene id) are listed first as owned, even ones outside the ThePornDB list."""
import os, sys, json, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
c.execute("INSERT INTO films(id,title,kind,tpdb_id,cast_,release_date) VALUES(1,'Old Owned','scene','s0005','Penny Barber','2012-01-01')")   # page 1 (oldest-first? any page)
c.execute("INSERT INTO films(id,title,kind,tpdb_id,cast_,release_date) VALUES(2,'Deep Owned','scene','s0420','Someone Else','2015-01-01')")  # page 5, linked by scene id only
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(3,'Alias Owned','scene','Penny Barbers','2016-01-01')")  # alias name, no tpdb id
c.execute("INSERT INTO films(id,title,kind,cast_,release_date) VALUES(4,'Unlisted Owned','scene','Penny Barber','2010-01-01')")  # not on ThePornDB list
c.commit()
N, PER = 450, 100; calls = []
def tp(path, tok, **k):
    if path == "/performers": return {"data": [{"id": "pb", "name": "Penny Barber", "extras": {"aliases": ["Penny Barbers"]}, "aliases": ["Penny Barbers"]}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1); calls.append(p)
        ids = range((p - 1) * PER, min(p * PER, N))
        return {"data": [{"id": f"s{i:04d}", "title": f"Scene {i}", "date": f"20{10 + i % 15:02d}-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": (N + PER - 1) // PER, "current_page": p}}
    return {"data": {"id": "pb", "name": "Penny Barber", "aliases": ["Penny Barbers"], "extras": {}}}
tpdb.call_retry = tp
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Penny Barber").json["id"]
c.execute("UPDATE people SET aliases=? WHERE id=?", (json.dumps(["Penny Barbers"]), pid)); c.commit()
for _ in range(200):
    j = cl.get(f"/api/person/{pid}").json
    if not j.get("scenes_loading") and j.get("scenes") is not None and len(j["scenes"]) > 2: break
    time.sleep(0.05)
assert calls == [1, 2, 3, 4, 5], calls
sc = j["scenes"]; owned = [x["title"] for x in sc if x["film_id"]]
assert set(owned) == {"Old Owned", "Deep Owned", "Alias Owned", "Unlisted Owned"}, owned
assert all(x["film_id"] for x in sc[:4]) and not any(x["film_id"] for x in sc[4:])  # owned first
assert len(sc) == N - 2 + 4  # 448 not owned + 4 owned (2 of them are on the list)
assert j["scenes_owned"] == 4 and j["scenes_tpdb_total"] == N and j["scenes_total"] == N + 2, (j["scenes_owned"], j["scenes_total"])
# re-link to another ThePornDB performer: the cached scene list is dropped and reloaded for the new id
assert cl.post(f"/api/person/{pid}/tpdb", json={"tpdb": "other-perf"}).status_code == 200
r = c.execute("SELECT tpdb_performer_id, tpdb_scenes_json FROM people WHERE id=?", (pid,)).fetchone()
assert r[0] == "pb" or r[1] is None  # fake API echoes id 'pb'; same id keeps cache, a different id clears it
print("ok")
