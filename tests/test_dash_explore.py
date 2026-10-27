"""Dashboard scopes and explore drill-downs: /api/dash/explore totals equal /api/films totals for the same filters;
month buckets (year-only dates = unknown month); scope tiles limit the dashboard; adult titles only count performers
with a known birth date who were 18+ at release. Temp DB, no network. Run: python tests/test_dash_explore.py"""
import os, sys, tempfile, urllib.parse
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
root = os.path.join(tempfile.mkdtemp(), "Movies")
db.set_setting(c, "roots", [root])
def film(t, y, d="", co="", pg="", tags=(), kind=None, st=None, cast="", rd=None, path=None):
    i = c.execute("INSERT INTO films(title,year,director,country,primary_genre,kind,studio,cast_,release_date) VALUES(?,?,?,?,?,?,?,?,?)",
                  (t, y, d, co, pg, kind, st, cast, rd)).lastrowid
    if path: c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,?)", (i, os.path.join(root, path), 10))
    for n in tags:
        c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES(?,?)", (n, "Content"))
        c.execute("INSERT INTO film_tags(film_id,tag_id) VALUES(?,(SELECT id FROM tags WHERE name=?))", (i, n))
    return i
film("Alpha", 1975, "Ann Lee", "France", "Drama", ["Noir"], rd="1975-03-02", path="Alpha (1975)/a.mkv", cast="Bob Ray")
film("Beta", 1975, "Ann Lee", "France, Italy", "drama", ["Noir"], rd="1975", path="Beta (1975)/b.mkv")
film("Gamma", 1975, "Cy Fox", "Italy", "Horror", rd="1975-03-30", path="Gamma (1975)/g.mkv")
film("Delta", 1982, "Ann Lee", "Italy", "Horror", ["Noir"], rd="1982-11-01")
film("Eps", 1978, "Cy Fox", "USA", "Hardcore", rd="1978-05-05", cast="Old Enough, No Date")
film("Zeta", 1979, "Cy Fox", "USA", "Hardcore", rd="1979-01-01", cast="Old Enough")
film("S1", 2020, kind="scene", st="Studio X", cast="Old Enough", rd="2020-05-01")
film("S2", 2021, kind="scene", st="Studio X", rd="2021-06")
film("Undated", None, "Cy Fox", "", "")
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Old Enough','old enough','1950-01-01')")
c.commit()
cl = app.app.test_client()
def both(**q):
    qs = urllib.parse.urlencode(q)
    ex = cl.get("/api/dash/explore?" + qs).json; li = cl.get("/api/films?limit=1&" + qs).json
    assert ex["total"] == li["total"], (q, ex["total"], li["total"])
    return ex
ex = both()
assert ex["total"] == 9 and ex["films"] == 7 and ex["scenes"] == 2 and ex["undated"] == 1
ex = both(decade="1970")
assert [(y["k"], y["films"]) for y in ex["years"]] == [(1975, 3), (1978, 1), (1979, 1)]
ex = both(year_min=1975, year_max=1975)
assert {m["k"]: m["films"] for m in ex["months"]} == {0: 1, 3: 2}, ex["months"]
both(year_min=1975, year_max=1975, month=3); both(year_min=1975, year_max=1975, month=0)
assert both(year_min=1975, year_max=1975, month=3)["total"] == 2
ex = both(pgenre="Drama", films_only=1)
assert ex["total"] == 2 and ex["genres"][0]["n"] == 2  # 'Drama' and 'drama' are one genre (same as the filter)
assert [x["name"] for x in ex["tags"]] == ["Noir"]
ex = both(country="Italy", films_only=1); assert ex["total"] == 3
ex = both(person="Ann Lee", prole="director", films_only=1)
assert ex["total"] == 3 and ex["year_min"] == 1975 and ex["year_max"] == 1982
both(pgenre="Horror", decade="1980", films_only=1); both(all="Noir", country="France")
both(studio="Studio X", scenes=1)
ex = both(pgenre="Hardcore", films_only=1)
assert [(x["name"], x["n"]) for x in ex["cast"]] == [("Old Enough", 2)], ex["cast"]  # 'No Date' left out (no birth date)
ex = both(lib=root); assert ex["total"] == 3
# scopes: tiles keep whole-library numbers, everything else follows the scope
d = cl.get("/api/dashboard?force=1&scope=movies").json
assert d["scope"] == "movies" and d["stats"]["films"] == 7 and d["stats"]["in_scope"] == 3 and d["stats"]["movies"] == 3
assert sum(x["films"] + x["scenes"] for x in d["decades"]) == 3 and d["scope_filter"] == {"lib": root}
d = cl.get("/api/dashboard?force=1&scope=scenes").json
assert d["stats"]["in_scope"] == 2 and d["studios"][0]["name"] == "Studio X" and not d["genres"]
d = cl.get("/api/dashboard?force=1&scope=classic").json
assert d["stats"]["in_scope"] == 2 and d["genres"][0]["name"] == "Hardcore"
d = cl.get("/api/dashboard?force=1").json
assert d["scope"] == "" and d["stats"]["in_scope"] == 9 and set(d["scopes"]) == {"films", "movies", "classic", "scenes"}
ex = cl.get("/api/dash/explore?scope=classic&decade=1970").json; assert ex["total"] == 2
print("ok")
