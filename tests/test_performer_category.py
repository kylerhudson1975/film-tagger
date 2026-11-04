"""Performers page Mainstream / Porn split: /api/people/built?cat=mainstream lists built mainstream profiles (TMDB filmography
loaded, credited only on non-adult titles); the default / cat=porn list is unchanged (adult titles only). Favorites and
people search carry adult=true/false so the page can filter by category. Run: python tests/test_performer_category.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "roots", [r"D:\Movies"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(1,'Sc','scene','Pia Porn, Both Ways','2018-01-01',2018)")
c.execute("INSERT INTO files(film_id,path) VALUES(1,'D:\\S\\Sc.mp4')")
c.execute("INSERT INTO films(id,title,cast_,release_date,year) VALUES(2,'Movie','Max Main, Both Ways, Nora Notopen','2010-01-01',2010)")
c.execute("INSERT INTO files(film_id,path) VALUES(2,'D:\\Movies\\Movie.mkv')")
c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Pia Porn", "Both Ways", "Max Main", "Nora Notopen")}
cr = json.dumps({"cast": [{"id": 5, "title": "Movie", "release_date": "2010-01-01", "character": "X"}, {"id": 6, "title": "Other", "release_date": "2012-05-01", "character": "Y"}], "crew": []})
c.execute("UPDATE people SET tmdb_person_id=11, credits_json=?, fetched_at=1 WHERE id=?", (cr, ids["Max Main"]))
c.execute("UPDATE people SET tmdb_person_id=12, credits_json=?, fetched_at=1 WHERE id=?", (cr, ids["Both Ways"]))  # on a scene too -> porn
c.execute("UPDATE people SET birthday='1990-01-01', tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?",
          (json.dumps([dict(id="p1", _id="", title="S", date="2017-01-01", studio="St", slug=None, thumb=None)]), ids["Pia Porn"]))
c.commit()
m = cl.get("/api/people/built?cat=mainstream").json
assert m["cat"] == "mainstream" and [p["name"] for p in m["people"]] == ["Max Main"], m
assert m["people"][0]["total"] >= 2, m
p = cl.get("/api/people/built").json
assert p["cat"] == "porn" and "Max Main" not in {x["name"] for x in p["people"]} and "Pia Porn" in {x["name"] for x in p["people"]}, p
assert cl.get("/api/people/built?cat=porn").json["people"] == p["people"]
for n in ("Max Main", "Pia Porn"): cl.post(f"/api/people/{ids[n]}/favorite", json={"favorite": True})
f = {x["name"]: x["adult"] for x in cl.get("/api/people/favorites").json["people"]}
assert f == {"Max Main": False, "Pia Porn": True}, f
for n in ("Max Main", "Pia Porn"): cl.post(f"/api/people/{ids[n]}/favorite", json={"favorite": False})
s = {x["name"]: x["adult"] for x in cl.get("/api/people/search?q=o").json["people"]}
assert s.get("Both Ways") is True and s.get("Nora Notopen") is False and s.get("Pia Porn") is True, s
print("ok")
