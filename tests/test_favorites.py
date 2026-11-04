"""Favorites: the person-page star (POST /api/people/<id>/favorite) and the Favorites list (GET /api/people/favorites),
A-Z by first name then last name, with library scene counts. Migration is idempotent on old and fresh databases; the star
never touches hand-edited fields. Run: python tests/test_favorites.py"""
import os, sys, json, sqlite3, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# an existing (pre-favorites) database: people table without the new columns
old = sqlite3.connect(os.environ["FILMTAGGER_DB"])
old.executescript("CREATE TABLE people(id INTEGER PRIMARY KEY, tmdb_person_id INTEGER, name TEXT NOT NULL, name_norm TEXT UNIQUE, profile_path TEXT, bio TEXT, "
                  "birthday TEXT, deathday TEXT, place TEXT, fetched_at REAL, known_for TEXT, credits_json TEXT, tmdb_manual INTEGER DEFAULT 0);"
                  "INSERT INTO people(name,name_norm,bio) VALUES('Old Timer','old timer','kept');")
old.commit(); old.close()
import app, db, people as P
cl = app.app.test_client()
c = db.connect()
for _ in range(2): P.migrate(c)  # idempotent
cols = {r[1] for r in c.execute("PRAGMA table_info(people)")}
assert {"favorite", "favorite_at"} <= cols, cols
assert c.execute("SELECT favorite, bio FROM people WHERE name_norm='old timer'").fetchone()[:] == (0, "kept")
# fresh install path (installer runs the same app code): new DB gets the columns too
fresh = sqlite3.connect(os.path.join(d, "fresh.db")); fresh.row_factory = sqlite3.Row
fresh.executescript(P.PEOPLE_SCHEMA); P.migrate(fresh); P.migrate(fresh)
assert {"favorite", "favorite_at"} <= {r[1] for r in fresh.execute("PRAGMA table_info(people)")}
fresh.close()

db.set_setting(c, "scene_roots", [r"D:\Porn Scenes"])
def scene(title, cast, kind="scene"):
    fid = c.execute("INSERT INTO films(title,kind,cast_) VALUES(?,?,?)", (title, kind, cast)).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, rf"D:\Porn Scenes\{title}.mp4"))
scene("S1", "Zoe Adams, Anna Zed"); scene("S2", "Anna Zed"); scene("S3", "Anna Baker"); scene("M1", "Anna Zed", kind=None)
c.commit()
pid = lambda n: cl.get("/api/person/lookup?name=" + n).json["id"]
za, az, ab, ot = pid("Zoe Adams"), pid("Anna Zed"), pid("Anna Baker"), pid("Old Timer")
assert cl.get("/api/people/favorites").json == {"people": [], "total": 0}
# hand edits on one person: the star must not touch them
assert cl.post(f"/api/person/{az}/edit", json={"bio": "my words", "height": "170"}).json["ok"]
before = dict(c.execute("SELECT * FROM people WHERE id=?", (az,)).fetchone())
for p_ in (za, az, ab):
    r = cl.post(f"/api/people/{p_}/favorite", json={"favorite": True}).json
    assert r["ok"] and r["favorite"] is True and r["id"] == p_
after = dict(c.execute("SELECT * FROM people WHERE id=?", (az,)).fetchone())
assert after["favorite"] == 1 and after["favorite_at"]
assert {k: v for k, v in after.items() if k not in ("favorite", "favorite_at")} == {k: v for k, v in before.items() if k not in ("favorite", "favorite_at")}
assert json.loads(after["edited_fields"]) == json.loads(before["edited_fields"])
f = cl.get("/api/people/favorites").json
assert [p["name"] for p in f["people"]] == ["Anna Baker", "Anna Zed", "Zoe Adams"], f   # first name, then last name
byn = {p["name"]: p for p in f["people"]}
assert byn["Anna Zed"]["scenes"] == 2 and byn["Anna Zed"]["titles"] == 1 and byn["Zoe Adams"]["scenes"] == 1
assert cl.get(f"/api/person/{az}").json["person"]["favorite"] is True
assert cl.get(f"/api/person/{ot}").json["person"]["favorite"] is False
# idempotent set, toggle without body, string values
assert cl.post(f"/api/people/{az}/favorite", json={"favorite": True}).json["favorite"] is True
assert cl.post(f"/api/people/{az}/favorite").json["favorite"] is False
assert cl.post(f"/api/people/{az}/favorite", json={"favorite": "true"}).json["favorite"] is True
assert cl.post(f"/api/people/{az}/favorite", json={"favorite": False}).json["favorite"] is False
assert [p["name"] for p in cl.get("/api/people/favorites").json["people"]] == ["Anna Baker", "Zoe Adams"]
assert cl.post("/api/people/999999/favorite", json={"favorite": True}).status_code == 404
# star shows in the people search
s = {p["name"]: p for p in cl.get("/api/people/search?q=anna&limit=50").json["people"]}
assert s["Anna Baker"]["fav"] is True and s["Anna Zed"]["fav"] is False
# merge: a starred duplicate hands its star to the kept person; un-starring the kept person clears both
assert cl.post(f"/api/person/{az}/merge", json={"other": ab}).json["ok"]
names = [p["name"] for p in cl.get("/api/people/favorites").json["people"]]
assert names == ["Anna Zed", "Zoe Adams"], names
assert cl.get(f"/api/person/{az}").json["person"]["favorite"] is True
assert cl.post(f"/api/people/{ab}/favorite", json={"favorite": False}).json == {"ok": True, "id": az, "favorite": False}
assert [p["name"] for p in cl.get("/api/people/favorites").json["people"]] == ["Zoe Adams"]
# no tags were changed by any of this
assert c.execute("SELECT COUNT(*) FROM film_tags").fetchone()[0] == 0
print("ok")
