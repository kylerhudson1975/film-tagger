"""People search scoped to a library folder (?lib=): only credits on titles in that folder count; adult titles follow the
age rule (performers need a known birth date and 18+ at release). Run: python tests/test_people_search_lib.py"""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
cl = app.app.test_client()
c = db.connect()
db.set_setting(c, "roots", [r"D:\Movies"]); db.set_setting(c, "scene_roots", [r"D:\Porn Scenes"])
def add(title, kind, cast, path, director="", year=2020, rd="2020-05-01"):
    fid = c.execute("INSERT INTO films(title,kind,cast_,director,year,release_date) VALUES(?,?,?,?,?,?)", (title, kind, cast, director, year, rd)).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path))
add("Groundhog Day", "film", "Bill Murray, Andie MacDowell", r"D:\Movies\Groundhog Day.mkv", "Harold Ramis", 1993, "1993-02-12")
add("Love Actually", "film", "Bill Nighy", r"D:\Movies\Love Actually.mkv", "Richard Curtis", 2003, "2003-11-14")
add("Scene One", "scene", "Bill Bailey Star, Billie Dated", r"D:\Porn Scenes\Studio\Scene One.mp4", "Bill Directorman")
add("Scene Two", "scene", "Billie Dated", r"D:\Porn Scenes\Studio\Scene Two.mp4")
c.commit()
pid = cl.get("/api/person/lookup?name=Billie Dated").json["id"]
c.execute("UPDATE people SET birthday='1995-01-01' WHERE id=?", (pid,)); c.commit()
names = lambda **a: {p["name"]: p["n"] for p in cl.get("/api/people/search", query_string=dict(q="bill", **a)).json["people"]}
allp = names()
assert {"Bill Murray", "Bill Nighy", "Billie Dated", "Bill Bailey Star", "Bill Directorman"} <= set(allp), allp
assert allp["Billie Dated"] == 2
sc = cl.get("/api/people/search", query_string=dict(q="bill", lib=r"D:\Porn Scenes")).json
got = {p["name"]: p["n"] for p in sc["people"]}
assert got == {"Billie Dated": 2, "Bill Directorman": 1}, got   # no Bill Murray; no-birth-date performer left out
assert sc["age_left_out"] == 1 and sc["total"] == 2
assert names(lib=r"D:\Porn Scenes\Studio") == got
assert names(lib=r"D:\Movies") == {"Bill Murray": 1, "Bill Nighy": 1}
assert names(lib=r"D:\Elsewhere") == {}
assert names() == allp  # unscoped unchanged
print("ok")
