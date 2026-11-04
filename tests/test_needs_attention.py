"""Performers > Needs attention (/api/people/needs_attention): porn = people on adult titles missing birth date / photo /
gender / ThePornDB-or-TMDB link, most scenes first; mainstream = opened profiles missing birth date / photo / link; counts per
missing field; complete people left out. Run: python tests/test_needs_attention.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "roots", [r"D:\Movies"])
for i, cast in enumerate(["Ann Few", "Bea Many, Ann Few", "Bea Many", "Bea Many, Cat Done"], 1):
    c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(?,?,?,?)", (i, f"S{i}", "scene", cast))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\S{i}.mp4"))
c.execute("INSERT INTO films(id,title,cast_) VALUES(9,'Movie','Max Main, Mo Unopened')"); c.execute("INSERT INTO files(film_id,path) VALUES(9,'D:\\Movies\\M.mkv')")
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Ann Few", "Bea Many", "Cat Done", "Max Main", "Mo Unopened")}
c.execute("UPDATE people SET birthday='1990-01-01', image_url='https://e/a.jpg', source='tpdb', gender='Female', tpdb_performer_id='x' WHERE id=?", (ids["Cat Done"],))
c.execute("UPDATE people SET birthday='1990-01-01', tpdb_performer_id='y', gender='Female' WHERE id=?", (ids["Bea Many"],))
c.execute("UPDATE people SET tpdb_scenes_total=50 WHERE id=?", (ids["Ann Few"],))
c.execute("UPDATE people SET tmdb_person_id=5, credits_json='{\"cast\":[],\"crew\":[]}', fetched_at=1 WHERE id=?", (ids["Max Main"],))
c.commit()
p = cl.get("/api/people/needs_attention").json
assert p["cat"] == "porn" and [x["name"] for x in p["people"]] == ["Ann Few", "Bea Many"], p   # 50 ThePornDB scenes beat 3 library ones
m = {x["name"]: x["missing"] for x in p["people"]}
assert m == {"Ann Few": ["birth", "photo", "gender", "link"], "Bea Many": ["photo"]}, m
assert p["counts"] == {"birth": 1, "photo": 2, "gender": 1, "link": 1} and p["total"] == 2
ms = cl.get("/api/people/needs_attention?cat=mainstream").json
assert [(x["name"], x["missing"]) for x in ms["people"]] == [("Max Main", ["birth", "photo"])], ms
print("ok")
