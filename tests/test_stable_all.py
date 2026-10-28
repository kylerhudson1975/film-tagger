"""Stable / Muses / Harem 'All films': the director's TMDB filmography plus the cast of each film not in the library
(TMDB movie credits, top 10, cached in tmdb_movie_cast), counted as 'N total (M in library)'; the age rule still applies.
TMDB is mocked (no network). Run: python tests/test_stable_all.py"""
import os, sys, json, tempfile, time, io, urllib.request
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
def film(t, y, d, cast, tmdb, pg="Comedy"):
    return c.execute("INSERT INTO films(title,year,director,cast_,primary_genre,tmdb_id,release_date) VALUES(?,?,?,?,?,?,?)", (t, y, d, cast, pg, tmdb, f"{y}-06-01")).lastrowid
# library: 3 films directed by Wes Test with Bill Test; 4 with Owen Test (Owen also on one more TMDB film)
for i, y in enumerate([1996, 1998, 2001]): film(f"W{i}", y, "Wes Test", "Bill Test, Owen Test", 100 + i)
film("W3", 2004, "Wes Test", "Owen Test, Anjelica Test", 103)
DIRECTED = [100, 101, 102, 103, 104, 105, 106, 107, 108]   # 5 more not in the library (104-108)
cred = dict(cast=[], crew=[dict(id=t, title=f"T{t}", release_date=f"{1995 + t - 100}-05-05", job="Director", department="Directing") for t in DIRECTED])
c.execute("INSERT INTO people(name,name_norm,tmdb_person_id,credits_json,fetched_at,source) VALUES('Wes Test','wes test',7,?,?, 'tmdb')", (json.dumps(cred), time.time()))
CAST = {104: ["Bill Test", "Owen Test"], 105: ["Bill Test", "Anjelica Test"], 106: ["Bill Test", "Anjelica Test"],
        107: ["Bill Test", "Anjelica Test", "Extra " + "x" * 3], 108: ["Bill Test", "Jason Test"]}
db.set_setting(c, "tmdb_key", "k" * 32); c.commit()
calls = []
real = urllib.request.urlopen
def fake(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if "api.themoviedb.org" not in url: return real(req, timeout=timeout)
    calls.append(url)
    t = int(url.split("/movie/")[1].split("/")[0])
    body = json.dumps({"cast": [dict(name=n, id=1000 + k, order=k) for k, n in enumerate(CAST.get(t, []))]}).encode()
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return R(body)
urllib.request.urlopen = fake
people.SLEEP = 0
cl = app.app.test_client()
wes = c.execute("SELECT id FROM people WHERE name='Wes Test'").fetchone()[0]
# library mode: only Owen reaches 4 (Bill has 3 in the library)
ins = cl.get(f"/api/people/{wes}/insights").json["stable"]
assert [(x["name"], x["n"]) for x in ins["directed"]] == [("Owen Test", 4)], ins
# all films: loads the 5 missing casts once (rate-limited job), then counts the whole filmography
r = cl.get(f"/api/people/{wes}/stable_all").json
assert r["status"] == "loading", r
for _ in range(100):
    time.sleep(0.2); r = cl.get(f"/api/people/{wes}/stable_all").json
    if r["status"] == "ok": break
assert r["status"] == "ok", r
st = r["stable"]; got = {x["name"]: (x["n"], x["lib"], x["tier"]) for x in st["directed"]}
assert got == {"Bill Test": (8, 3, "harem"), "Owen Test": (5, 4, "muses"), "Anjelica Test": (4, 1, "stable")}, got
assert st["films"]["directed"] == 9 and st["films_lib"]["directed"] == 4 and r["missing_casts"] == 0 and r["has_filmography"]
assert sorted(int(u.split("/movie/")[1].split("/")[0]) for u in calls) == [104, 105, 106, 107, 108]
assert c.execute("SELECT COUNT(*) FROM tmdb_movie_cast").fetchone()[0] == 5
n = len(calls); people_SJOB_cleared = True
r2 = cl.get(f"/api/people/{wes}/stable_all?refresh=1").json   # cached casts: no new TMDB calls even on refresh
for _ in range(50):
    if r2["status"] == "ok": break
    time.sleep(0.2); r2 = cl.get(f"/api/people/{wes}/stable_all").json
assert len(calls) == n and r2["stable"]["directed"] == st["directed"]
# age rule: the same filmography as adult titles; Bill has no birth date -> left out; Owen born 1990 -> only 18+ titles
c.execute("UPDATE films SET primary_genre='Hardcore'")
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Owen Test','owen test','1980-01-01')")
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Anjelica Test','anjelica test','1950-01-01')"); c.commit()
for _ in range(50):
    r3 = cl.get(f"/api/people/{wes}/stable_all").json
    if r3["status"] == "ok" and r3["stable"] != st: break
    time.sleep(0.2)
got = {x["name"]: (x["n"], x["lib"]) for x in r3["stable"]["directed"]}
assert "Bill Test" not in got and got["Owen Test"] == (4, 3) and got["Anjelica Test"] == (4, 1), got   # Owen 18 in 1998: 1996 film excluded
assert r3["stable"]["no_birthday"] >= 1
print("OK test_stable_all")
