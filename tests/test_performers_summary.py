"""Performers page side panel (/api/people/<id>/summary): total and years active, busiest year, credits per year (library vs
not), top studios (hidden spellings merged), top co-stars, the age rule, the star; cached until the person's data changes;
never calls ThePornDB/TMDB itself (an unbuilt person stays unbuilt until the person page is opened), says loading:true and
is never cached while the scene list is still loading, and reports the full figures once the build finishes.
Run: python tests/test_performers_summary.py"""
import os, sys, json, tempfile, threading, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tpdb_key", "t")
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year,tpdb_id) VALUES(1,'Lib1','scene','Cora Chase, Mia Jay','Taboo Heat','2020-01-01',2020,'cc0')")
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(2,'Lib2','scene','Cora Chase, Mia Jay','taboo-heat','2019-05-01',2019)")
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(3,'Lib3','scene','Nob Day, Cora Chase','Other','2021-01-01',2021)")
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(4,'Fresh','scene','Tess New','2012-03-01',2012)")
for i in (1, 2, 3, 4): c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, f"D:\\S\\L{i}.mp4"))
c.commit()
calls = []
real = tpdb.call_retry
def guard(path, tok, **k):
    calls.append(path); raise RuntimeError("no network in tests")
tpdb.call_retry = guard
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Cora Chase", "Mia Jay", "Nob Day", "Tess New")}
lst = [dict(id=f"c{k}", _id="", title=f"S{k}", date=dt, studio=st, slug=None, thumb=None) for k, (st, dt) in enumerate(
    [("Taboo Heat", "2019-01-01")] * 3 + [("Jerky Wives", "2018-01-01")] * 2 + [("Kinky Cory", "2017-01-01")] * 1)]
lst.append(dict(id="cc0", _id="", title="Lib1", date="2020-01-01", studio="Taboo Heat", slug=None, thumb=None))
c.execute("UPDATE people SET birthday='1985-01-01', tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?", (json.dumps(lst), ids["Cora Chase"]))
c.execute("UPDATE people SET birthday='1990-01-01' WHERE id=?", (ids["Mia Jay"],))
db.add_studio_alias(c, "taboo-heat", "Taboo Heat", apply=False)
c.commit()
s = cl.get(f"/api/people/{ids['Cora Chase']}/summary").json
assert s["name"] == "Cora Chase" and s["built"] is True and s["adult"] is True and s["favorite"] is False and s["loading"] is False
# 6 ThePornDB-only + Lib1 (same tpdb id as cc0) + Lib2 + Lib3 = 9
assert s["total"] == 9 and s["in_library"] == 3, (s["total"], s["in_library"])
assert (s["first"], s["last"]) == (2017, 2021)
assert s["busiest"] == {"year": 2019, "n": 4}, s["busiest"]   # 3 ThePornDB + Lib2
assert {y["k"]: (y["lib"], y["other"]) for y in s["years"]} == {2017: (0, 1), 2018: (0, 2), 2019: (1, 3), 2020: (1, 0), 2021: (1, 0)}, s["years"]
assert s["studios"][0] == {"name": "Taboo Heat", "n": 5}, s["studios"]   # 'taboo-heat' merged into Taboo Heat
assert [x["name"] for x in s["studios"]] == ["Taboo Heat", "Jerky Wives", "Kinky Cory", "Other"]
co = {x["name"]: x["n"] for x in s["costars"]}
assert co.get("Mia Jay") == 2 and "Nob Day" not in co, s["costars"]   # Nob Day has no birth date: no counted adult scenes
assert s["cached"] is False and cl.get(f"/api/people/{ids['Cora Chase']}/summary").json["cached"] is True
# the star shows and a change is seen at once
cl.post(f"/api/people/{ids['Cora Chase']}/favorite", json={"favorite": True})
s2 = cl.get(f"/api/people/{ids['Cora Chase']}/summary").json; assert s2["favorite"] is True and s2["cached"] is False
cl.post(f"/api/people/{ids['Cora Chase']}/favorite", json={"favorite": False})
# person data change -> recomputed
lst.append(dict(id="c99", _id="", title="New", date="2022-01-01", studio="Jerky Wives", slug=None, thumb=None))
c.execute("UPDATE people SET tpdb_scenes_json=? WHERE id=?", (json.dumps(lst), ids["Cora Chase"])); c.commit()
s3 = cl.get(f"/api/people/{ids['Cora Chase']}/summary").json
assert s3["cached"] is False and s3["total"] == 10 and s3["last"] == 2022
# age rule: no birth date -> nothing adult counts, and the panel says why
n = cl.get(f"/api/people/{ids['Nob Day']}/summary").json
assert n["total"] == 0 and n["excluded"] == 1 and n["excluded_why"] == "no_birthday" and n["built"] is False, n
assert cl.get("/api/people/999999/summary").status_code == 404
assert calls == [], calls   # the summary never calls ThePornDB
# unbuilt person: summary alone does not build; opening the person page does (background load), summary follows it
s = cl.get(f"/api/people/{ids['Tess New']}/summary").json
assert s["built"] is False and s["loading"] is False and calls == []
assert c.execute("SELECT tpdb_scenes_json FROM people WHERE id=?", (ids["Tess New"],)).fetchone()[0] is None
c.execute("UPDATE people SET birthday='1990-01-01' WHERE id=?", (ids["Tess New"],)); c.commit()
N, PER = 250, 100
gate = threading.Event()
def tp(path, tok, **k):
    calls.append(path)
    if path == "/performers": return {"data": [{"id": "tn", "name": "Tess New", "extras": {}}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1)
        if p == 3: gate.wait(20)
        return {"data": [{"id": f"t{i:04d}", "title": f"T{i}", "date": f"{2013 + i % 5}-01-01", "site": {"name": "Site " + str(i % 3)}} for i in range((p - 1) * PER, min(p * PER, N))],
                "meta": {"total": N, "last_page": 3, "current_page": p}}
    return {"data": {"id": "tn", "name": "Tess New", "extras": {}}}
tpdb.call_retry = tp
assert cl.get(f"/api/person/{ids['Tess New']}").json["scenes_loading"]
for _ in range(200):
    a = cl.get(f"/api/people/{ids['Tess New']}/summary").json
    if a["loading"]: break
    time.sleep(0.05)
assert a["loading"] is True and a["cached"] is False
assert cl.get(f"/api/people/{ids['Tess New']}/summary").json["cached"] is False   # never cached while loading
gate.set()
for _ in range(400):
    f = cl.get(f"/api/people/{ids['Tess New']}/summary").json
    if not f["loading"]: break
    time.sleep(0.05)
f = cl.get(f"/api/people/{ids['Tess New']}/summary").json
assert f["built"] is True and f["loading"] is False and f["total"] == N + 1, (f["total"], f["built"])
assert (f["first"], f["last"]) == (2012, 2017) and len(f["studios"]) >= 3
assert cl.get(f"/api/people/{ids['Tess New']}/summary").json["cached"] is True
tpdb.call_retry = real
print("ok")
