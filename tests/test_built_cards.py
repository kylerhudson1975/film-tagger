"""Built profiles list (/api/people/built) carries what the photo cards need: photo URL (or none -> initials) and the
favorite star, read fresh on every call (starring after the figures are cached still shows). Run: python tests/test_built_cards.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
def scene(i, cast, y):
    c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(?,?,?,?,?,?)", (i, f"T{i}", "scene", cast, f"{y}-01-01", y))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\T{i}.mp4"))
scene(1, "Cara Card", 2015); scene(2, "Dee Deck", 2018); c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Cara Card", "Dee Deck")}
sc = lambda pre, ys: json.dumps([dict(id=f"{pre}{k}", _id="", title=f"S{k}", date=f"{y}-01-01", studio="S", slug=None, thumb=None) for k, y in enumerate(ys)])
c.execute("UPDATE people SET birthday='1990-01-01', source='tpdb', image_url='https://e/x.jpg', tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?", (sc("c", [2016, 2016, 2017]), ids["Cara Card"]))
c.execute("UPDATE people SET birthday='1990-01-01', tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?", (sc("d", [2018, 2020]), ids["Dee Deck"]))
c.commit()
b = {p["name"]: p for p in cl.get("/api/people/built").json["people"]}
assert set(b) == {"Cara Card", "Dee Deck"}, b
assert b["Cara Card"]["photo"] and b["Cara Card"]["photo"].startswith(f"/api/person/{ids['Cara Card']}/photo") and b["Dee Deck"]["photo"] is None
assert b["Cara Card"]["total"] == 4 and b["Cara Card"]["busiest_year"] == 2016 and b["Cara Card"]["busiest_n"] == 2
assert not b["Cara Card"]["fav"]
cl.post(f"/api/people/{ids['Cara Card']}/favorite", json={"favorite": True})
b = {p["name"]: p for p in cl.get("/api/people/built").json["people"]}   # cached figures, fresh star
assert b["Cara Card"]["fav"] is True and b["Dee Deck"]["fav"] is False
cl.post(f"/api/people/{ids['Cara Card']}/favorite", json={"favorite": False})
print("ok")
