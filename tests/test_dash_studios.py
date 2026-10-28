"""Dashboard 'Top studios' in every scope (Films, Movies, Classic Porn, Scenes) and in explore: a title with several
studios counts once for each, hidden aliases count under the main name, titles with no studio are left out (no_studio),
and every bar's count equals the library list for scope + studio. Temp DB, no network. Run: python tests/test_dash_studios.py"""
import os, sys, tempfile, urllib.parse
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, reports as R
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
root = os.path.join(tempfile.mkdtemp(), "Movies")
db.set_setting(c, "roots", [root])
def film(t, y, pg="", kind=None, st=None, path=None, rd=None):
    i = c.execute("INSERT INTO films(title,year,primary_genre,kind,studio,release_date) VALUES(?,?,?,?,?,?)",
                  (t, y, pg, kind, st, rd)).lastrowid
    if path: c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,?)", (i, os.path.join(root, path), 10))
    return i
film("A", 1975, "Drama", st="Warner Bros., Amblin", path="A/a.mkv", rd="1975-03-01")
film("B", 1976, "Drama", st="warner bros", path="B/b.mkv", rd="1976-04-01")
film("C", 1977, "Horror", st="Amblin Entertainment", path="C/c.mkv")
film("D", 1978, "Horror", path="D/d.mkv")                       # no studio
film("E", 1978, "Hardcore", st="VCA, Caballero")
film("F", 1979, "Hardcore", st="vca.pictures")                   # alias of VCA
film("G", 1980, "Hardcore", st="")
film("S1", 2020, kind="scene", st="Studio X")
film("S2", 2021, kind="scene", st="Studio X, Studio Y")
c.execute("INSERT INTO studio_aliases(alias, alias_key, canonical) VALUES('vca.pictures', ?, 'VCA')", (db.studio_key("vca.pictures"),))
c.commit()
cl = app.app.test_client()
def listed(**q): return cl.get("/api/films?limit=1&" + urllib.parse.urlencode(q)).json["total"]
def check(scope, want, no):
    d = cl.get(f"/api/dashboard?scope={scope}&force=1").json
    got = [(x["name"], x["n"]) for x in d["studios"]]
    assert got == want, (scope, got)
    assert d["no_studio"] == no, (scope, d["no_studio"])
    for name, n in got:  # bar == library list for scope filter + studio
        q = app.fk_api({**d["scope_filter"], "studio": name})
        assert listed(**q) == n, (scope, name, listed(**q), n)
        ex = cl.get(f"/api/dash/explore?scope={scope}&studio={urllib.parse.quote(name)}&force=1").json
        assert ex["total"] == n, (scope, name, ex["total"])
    return d
d = check("films", [("VCA", 2), ("Warner Bros.", 2), ("Amblin", 1), ("Amblin Entertainment", 1), ("Caballero", 1)], 2)
check("classic", [("VCA", 2), ("Caballero", 1)], 1)
check("movies", [("Warner Bros.", 2), ("Amblin", 1), ("Amblin Entertainment", 1)], 1)
check("scenes", [("Studio X", 2), ("Studio Y", 1)], 0)
# explore inside a scope: studios card data for films too, month/decade filters respected
ex = cl.get("/api/dash/explore?scope=films&decade=1970&force=1").json
assert [(x["name"], x["n"]) for x in ex["studios"]][:2] == [("VCA", 2), ("Warner Bros.", 2)] and ex["no_studio"] == 1, ex["studios"]
ex = cl.get("/api/dash/explore?scope=films&year_min=1975&year_max=1975&month=3&force=1").json
assert [(x["name"], x["n"]) for x in ex["studios"]] == [("Amblin", 1), ("Warner Bros.", 1)] and ex["total"] == 1
print("OK test_dash_studios")
