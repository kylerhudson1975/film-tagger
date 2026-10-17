import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
c = db.connect()
c.execute("INSERT INTO films(id,title,kind,studio) VALUES(1,'x','scene','Sis Loves Me')"); c.execute("INSERT INTO files(film_id,path) VALUES(1,'D:\\\\S\\\\Sis Loves Me\\\\SisLovesMe Surprise Visit Tristan Summers.mp4')")
c.execute("INSERT INTO films(id,title,kind,notes) VALUES(2,'y','scene','Review by hand: x')"); c.execute("INSERT INTO files(film_id,path) VALUES(2,'D:\\\\S\\\\A\\\\a b.mp4')")
db.set_setting(c, "tpdb_key", "t"); c.commit()
SC = {"id": "u1", "slug": "sislovesme-bone-voyage", "title": "Bone Voyage", "date": "2021-09-10", "site": {"name": "Sis Loves Me"},
      "performers": [{"name": "Tristan Summers"}], "poster": "https://img/p.jpg", "duration": 3719}
def fake(path, token, **q):
    if path == "/sites": return {"data": [{"name": "Sis Loves Me"}]}
    if path.startswith("/scenes/"): return {"data": SC}
    return {"data": [SC, dict(SC, id="u2", title="Other", performers=[])]}
tpdb.call_retry = fake; tpdb.call = fake
cl = app.app.test_client()
r = cl.get("/api/films/1/tpdb/candidates").json
assert r["results"][0]["id"] == "u1" and r["results"][0]["thumb"] and "Tristan" in r["query"], r
assert len({x["id"] for x in r["results"]}) == len(r["results"])
r2 = cl.get("/api/films/2/tpdb/candidates").json
assert r2["review"] and all(not x.get("thumb") for x in r2["results"])
u = cl.get("/api/tpdb/unmatched").json["scenes"]; assert {x["id"] for x in u} == {1, 2}
cl.post("/api/films/2/tpdb", json={"tpdb_id": "u1"})
assert not c.execute("SELECT poster FROM films WHERE id=2").fetchone()[0]  # review-flagged: no image applied
cl.post("/api/films/1/tpdb", json={"tpdb_id": "https://theporndb.net/scenes/sislovesme-bone-voyage"})
assert c.execute("SELECT tpdb_id, poster FROM films WHERE id=1").fetchone()[:] == ("u1", "https://img/p.jpg")
assert [x["id"] for x in cl.get("/api/tpdb/unmatched").json["scenes"]] == []
print("ok")
