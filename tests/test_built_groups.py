"""Built profiles grouped by studio / label (/api/people/built_groups): sites roll up to their label (Kyle's own studio
link first, then the ThePornDB parent stored in studio_info, then the label saved with the scene), hidden studio spellings
merge, a performer is listed where she has >= 3 scenes (and always under her top group), groups are ordered by distinct
scenes, the age rule applies, by=site keeps sites apart, and answers are cached until data changes.
Run: python tests/test_built_groups.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people as P
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
assert P.site_label({"name": "Taboo Heat", "parent": {"name": "Cory Chase Network"}}) == "Cory Chase Network"
assert P.site_label({"name": "X", "network": {"name": "X"}}) is None and P.site_label(None) is None
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year,tpdb_id) VALUES(1,'Lib1','scene','Cora Chase, Mia Jay','Taboo Heat','2020-01-01',2020,'cc0')")
c.execute("INSERT INTO files(film_id,path) VALUES(1,'D:\\S\\Lib1.mp4')")
for i, n in enumerate(("Amy Cam", "Kid Young", "Nob Day"), 2):
    c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(?,?,?,?,?,?,?)", (i, f"L{i}", "scene", n, "Other", "2021-01-01", 2021))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, f"D:\\S\\L{i}.mp4"))
c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Cora Chase", "Mia Jay", "Amy Cam", "Kid Young", "Nob Day")}
def scenes(pid, spec, bd="1985-01-01"):
    lst = []
    for k, (studio, label, date) in enumerate(spec):
        lst.append(dict(id=f"{pid}-{k}", _id="", title=f"S{k}", date=date, studio=studio, label=label, slug=None, thumb=None))
    c.execute("UPDATE people SET birthday=?, tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?", (bd, json.dumps(lst), pid))
# Cora: Taboo Heat (ThePornDB parent known) x4 incl. the library scene by tpdb id, Jerky Wives (Kyle's link) x3,
#       Kinky Cory (label saved with the scene) x3, 'clips4sale: cory' x2 (below the minimum)
cc = [("Taboo Heat", None, "2019-01-01")] * 3 + [("Jerky Wives", None, "2018-01-01")] * 3 + [("Kinky Cory", "Cory Chase Network", "2017-01-01")] * 3 + [("Clips4Sale: Cory", None, "2016-01-01")] * 2
scenes(ids["Cora Chase"], cc)
c.execute("UPDATE people SET tpdb_scenes_json=? WHERE id=?", (json.dumps(json.loads(c.execute("SELECT tpdb_scenes_json FROM people WHERE id=?", (ids["Cora Chase"],)).fetchone()[0]) + [dict(id="cc0", _id="", title="Lib1", date="2020-01-01", studio="Taboo Heat", slug=None, thumb=None)]), ids["Cora Chase"]))
scenes(ids["Mia Jay"], [("Jerky Wives", None, "2018-02-01")] * 3 + [("Big Site", None, "2019-01-01")] * 2)
scenes(ids["Amy Cam"], [("Amiees Fetish House", None, "2015-01-01")] * 5 + [("amiees-fetish-house", None, "2015-02-01")] * 1 + [("Taboo Heat", None, "2016-01-01")] * 1)
scenes(ids["Kid Young"], [("Taboo Heat", None, "2001-01-01")] * 5, bd="1990-01-01")   # all before 18: not counted
scenes(ids["Nob Day"], [("Taboo Heat", None, "2019-01-01")] * 5, bd=None)              # no birth date: not counted
import studios  # noqa: F401  (schema)
c.executescript(studios.SCHEMA)
c.execute("INSERT INTO studio_info(key,name,tpdb_parent) VALUES('tabooheat','Taboo Heat','Cory Chase Network')")
db.add_studio_link(c, "Jerky Wives", "Cory Chase Network")
db.add_studio_alias(c, "amiees-fetish-house", "Amiees Fetish House", apply=False)
c.commit()
r = cl.get("/api/people/built_groups").json
assert r["by"] == "label" and r["min"] == 3 and r["cached"] is False
G = {g["name"]: g for g in r["groups"]}
cg = G["Cory Chase Network"]
# Cora 4 (3 + library) + 3 + 3 = 10 ; Mia 3 + the shared library scene = 4 ; Amy 1 (below min, not her top) ; distinct scenes 10 + 3 + 1 = 14
assert {p["id"]: p["n"] for p in cg["people"]} == {ids["Cora Chase"]: 10, ids["Mia Jay"]: 4}, cg
assert cg["scenes"] == 14, cg["scenes"]
assert [g["name"] for g in r["groups"]][0] == "Cory Chase Network"
af = G["Amiees Fetish House"]; assert af["people"] == [dict(id=ids["Amy Cam"], n=6)] and af["scenes"] == 6   # alias merged
assert "Clips4Sale: Cory" not in G and "Big Site" not in G   # below the minimum and not anyone's top group
where = lambda pid: {g["name"]: p["n"] for g in r["groups"] for p in g["people"] if p["id"] == pid}
assert where(ids["Kid Young"]) == {"Other": 1}, where(ids["Kid Young"])   # scenes before 18 never count; her adult library scene does
assert where(ids["Nob Day"]) == {}                                        # no birth date: nothing counts (like the card figures)
assert [g["scenes"] for g in r["groups"]] == sorted((g["scenes"] for g in r["groups"]), reverse=True)
assert cl.get("/api/people/built_groups").json["cached"] is True
s = {g["name"]: g for g in cl.get("/api/people/built_groups?by=site").json["groups"]}
assert {p["id"]: p["n"] for p in s["Taboo Heat"]["people"]} == {ids["Cora Chase"]: 4} and s["Jerky Wives"]["scenes"] == 6
assert "Cory Chase Network" not in s
# data change (a new studio link) -> recomputed
db.add_studio_link(c, "Amiees Fetish House", "Cory Chase Network"); c.commit()
r2 = cl.get("/api/people/built_groups").json
assert r2["cached"] is False and "Amiees Fetish House" not in {g["name"] for g in r2["groups"]}
assert {p["id"]: p["n"] for p in r2["groups"][0]["people"]}[ids["Amy Cam"]] == 7
print("ok")
