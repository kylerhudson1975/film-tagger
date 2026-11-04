"""Gender: Edit profile field (normalised, a protected hand edit), add-only fill from ThePornDB and TMDB (never replaces a
stored or hand-set value), the one-off backfill job (/api/people/gender_job) and Needs attention dropping the 'No gender'
badge once set (a hand-set 'Unknown' counts as set). Run: python tests/test_gender.py"""
import os, sys, io, json, time, tempfile, urllib.request, urllib.parse
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()  # before import: no automatic start-up run in the test
import app, tpdb, people as P
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tmdb_key", "k" * 32); db.set_setting(c, "tpdb_key", "t")
db.set_setting(c, "gender_backfill_done", 1)
names = ["Tm Woman", "Tp Woman", "Hand Set", "Has One", "No Data"]
for i, n in enumerate(names, 1):
    c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(?,?,?,?)", (i, f"S{i}", "scene", n))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\S{i}.mp4"))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in names}
c.execute("UPDATE people SET tmdb_person_id=101 WHERE id=?", (ids["Tm Woman"],))
c.execute("UPDATE people SET tpdb_performer_id='tp1' WHERE id=?", (ids["Tp Woman"],))
c.execute("UPDATE people SET tmdb_person_id=102 WHERE id=?", (ids["Hand Set"],))
c.execute("UPDATE people SET tmdb_person_id=103, gender='Male' WHERE id=?", (ids["Has One"],))
c.execute("UPDATE people SET tmdb_person_id=104 WHERE id=?", (ids["No Data"],)); c.commit()
# normalisation + hand edit
assert P.norm_gender("trans woman") == "Transgender Female" and P.norm_gender("non-binary") == "Non Binary" and P.norm_gender("x?") is None
r = cl.post(f"/api/person/{ids['Hand Set']}/edit", json={"gender": "male"}).json
assert r["ok"] and "gender" in r["edited"]
assert c.execute("SELECT gender FROM people WHERE id=?", (ids["Hand Set"],)).fetchone()[0] == "Male"
assert cl.get(f"/api/person/{ids['Hand Set']}").json["person"]["gender"] == "Male"
# apply_tpdb is add-only for gender
row = c.execute("SELECT * FROM people WHERE id=?", (ids["Has One"],)).fetchone()
P.apply_tpdb(c, row, {"id": "x", "extras": {"gender": "Female"}})
assert c.execute("SELECT gender FROM people WHERE id=?", (ids["Has One"],)).fetchone()[0] == "Male"
c.execute("UPDATE people SET tpdb_performer_id=NULL, source=NULL WHERE id=?", (ids["Has One"],)); c.commit()
# backfill job: TMDB 1 -> Female; ThePornDB; hand edit and stored value untouched; TMDB 0 -> nothing
G = {101: 1, 102: 1, 103: 1, 104: 0}; calls = []
def fake(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    assert "api.themoviedb.org" in url, url
    calls.append(url); pid = int(urllib.parse.urlparse(url).path.rsplit("/", 1)[1])
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return R(json.dumps(dict(id=pid, gender=G[pid])).encode())
urllib.request.urlopen = fake
tpdb.call_retry = lambda path, tok, **k: {"data": {"id": "tp1", "extras": {"gender": "Transgender Female"}}}
st = cl.post("/api/people/gender_job").json
for _ in range(100):
    st = cl.get("/api/people/gender_job").json
    if not st["running"]: break
    time.sleep(0.1)
g = {n: c.execute("SELECT gender FROM people WHERE id=?", (ids[n],)).fetchone()[0] for n in names}
assert g == {"Tm Woman": "Female", "Tp Woman": "Transgender Female", "Hand Set": "Male", "Has One": "Male", "No Data": None}, g
assert st["total"] == 3 and st["filled"] == 2 and st["tmdb"] == 1 and st["tpdb"] == 1 and st["none"] == 1, st
assert not any("/102" in u or "/103" in u for u in calls), calls
# Needs attention: 'No gender' only for No Data; Unknown by hand clears it
na = lambda: {p["name"]: p["missing"] for p in cl.get("/api/people/needs_attention").json["people"]}
assert [n for n, m in na().items() if "gender" in m] == ["No Data"], na()
cl.post(f"/api/person/{ids['No Data']}/edit", json={"gender": "Unknown"})
assert not any("gender" in m for m in na().values())
print("ok")
