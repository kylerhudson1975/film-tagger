"""'Credited with' (co-credits): /api/people/cocredit shared titles (library + cached TMDB filmography rows not in the
library), the age rule for adult titles, aliases, 3-person intersection, and cocredit= list filter counts matching.
Temp DB, no network. Run: python tests/test_cocredit.py"""
import os, sys, json, tempfile, urllib.parse
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
def film(t, y, d="", cast="", pg="Drama", co="", rd=None, kind=None, st=None, tmdb=None, rating=None, watched=0, writers=""):
    return c.execute("INSERT INTO films(title,year,director,cast_,writers,primary_genre,country,release_date,kind,studio,tmdb_id,rating,watched,runtime) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (t, y, d, cast, writers, pg, co, rd, kind, st, tmdb, rating, watched, 100)).lastrowid
film("One", 1980, "Ann Lee", "Maren Holt, Tobias Lind, Clara B", co="Sweden", tmdb=11, rating=8, watched=1)
film("Two", 1982, "Maren Holt", "Tobias Lind", co="Sweden, Denmark", tmdb=12)
film("Three", 1984, "Ann Lee", "Maren Holt, T. Lind, Clara B", pg="Thriller", co="France", rating=6)  # T. Lind = alias of Tobias
film("Solo", 1985, "Ann Lee", "Maren Holt")
film("Adult ok", 1990, "X Dir", "Maren Holt, Tobias Lind", pg="Hardcore", rd="1990-05-01")
film("Adult young", 1975, "X Dir", "Maren Holt, Tobias Lind", pg="Hardcore", rd="1975-05-01")  # Tobias born 1960: 15
film("Scene", 2001, "", "Maren Holt, Tobias Lind, Nobody Known", kind="scene", st="Studio X", rd="2001-02-02")
c.execute("INSERT INTO people(name,name_norm,birthday,aliases) VALUES('Maren Holt','maren holt','1955-03-01',NULL)")
c.execute("INSERT INTO people(name,name_norm,birthday,aliases) VALUES('Tobias Lind','tobias lind','1960-01-01','[\"T. Lind\"]')")
c.execute("INSERT INTO people(name,name_norm) VALUES('Clara B','clara b')")
cred = lambda ids: json.dumps(dict(cast=[dict(id=i, title=f"T{i}", release_date=f"{1970+i}-01-01") for i in ids], crew=[]))
c.execute("UPDATE people SET tmdb_person_id=1, credits_json=?, fetched_at=1 WHERE name='Maren Holt'", (cred([11, 12, 50, 51]),))
c.execute("UPDATE people SET tmdb_person_id=2, credits_json=?, fetched_at=1 WHERE name='Tobias Lind'", (cred([11, 50, 52]),))
c.commit()
pid = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM people")}
cl = app.app.test_client()
ids = f"{pid['Maren Holt']},{pid['Tobias Lind']}"
d = cl.get("/api/people/cocredit?ids=" + ids).json
titles = [r["title"] for r in d["rows"]]
# One, Two, Three (alias), Adult ok, Scene; T50 not in library; Adult young left out (Tobias 15)
assert titles == ["One", "Two", "Three", "Adult ok", "Scene", "T50"], titles
assert d["total"] == 6 and d["in_library"] == 5 and d["not_in_library"] == 1 and d["excluded"] == 1, (d["total"], d["excluded"])
assert d["adult_rule"] and d["rows"][0]["title"] == "One" and not d["rows"][-1]["in_library"]
assert [x["name"] for x in d["also"]] == ["Clara B"] and d["also"][0]["n"] == 2  # 'Nobody Known' (no birth date) skipped on the scene
assert d["directors"][0] == {"name": "Ann Lee", "n": 2, "is_person": None} and any(x["name"] == "Maren Holt" and x["is_person"] == 0 for x in d["directors"])
assert {x["name"]: x["n"] for x in d["countries"]} == {"Sweden": 2, "Denmark": 1, "France": 1}
assert d["ratings"] == {"avg": 7.0, "n": 2} and d["watched"] == {"n": 1, "of": 5}
two = next(r for r in d["rows"] if r["title"] == "Two"); assert two["roles"] == [["Director"], ["Actor"]]
p0 = d["people"][0]; assert p0["credits"] == 9 and p0["in_library"] == 7, p0  # 7 library + T50, T51
li = cl.get("/api/films?limit=50&cocredit=" + ids).json
assert li["total"] == 5 and {f["title"] for f in li["films"]} == {"One", "Two", "Three", "Adult ok", "Scene"}
assert cl.get("/api/films?limit=1&cocredit=" + ids + "&pgenre=Thriller").json["total"] == 1
# three people: intersection of all
d3 = cl.get(f"/api/people/cocredit?ids={ids},{pid['Clara B']}").json
assert d3["in_library"] == 2 and len(d3["people"]) == 3, d3["in_library"]
assert cl.get(f"/api/films?limit=1&cocredit={ids},{pid['Clara B']}").json["total"] == 2
# Clara has no birth date: nothing adult to leave out here, but a pair with adult titles shows the note
dn = cl.get(f"/api/people/cocredit?ids={pid['Maren Holt']},{pid['Clara B']}").json
assert dn["in_library"] == 2 and dn["excluded"] == 0
# no birth date for one person -> adult shared titles excluded with a note
c.execute("UPDATE people SET birthday=NULL WHERE name='Tobias Lind'"); c.commit()
d = cl.get("/api/people/cocredit?ids=" + ids).json
assert d["in_library"] == 3 and d["excluded"] == 4 and "no known birth date" in d["notes"][0], (d["in_library"], d["excluded"], d["notes"])
assert cl.get("/api/films?limit=1&cocredit=" + ids).json["total"] == 3
# suggest: people sharing films with Maren, best first; alias matches
s = cl.get(f"/api/people/cocredit/suggest?pid={pid['Maren Holt']}").json["people"]
assert s[0]["name"] == "Tobias Lind" and s[0]["n"] == 3 and (s[0]["n"], s[0]["total"]) == (d["in_library"], d["total"]) and s[1]["name"] == "Ann Lee" and "Nobody Known" not in [x["name"] for x in s], s
c.execute("UPDATE people SET birthday='1960-01-01' WHERE name='Tobias Lind'"); c.commit()
d = cl.get("/api/people/cocredit?ids=" + ids).json
s = cl.get(f"/api/people/cocredit/suggest?pid={pid['Maren Holt']}").json["people"]
assert (s[0]["n"], s[0]["total"]) == (d["in_library"], d["total"]) == (5, 6), (s[0], d["total"])  # same numbers as the page
s = cl.get(f"/api/people/cocredit/suggest?pid={pid['Maren Holt']}&q=t.+lind").json["people"]
assert s and s[0]["name"] == "Tobias Lind", s
assert cl.get("/api/people/cocredit?ids=" + str(pid['Maren Holt'])).status_code == 400
print("ok")
