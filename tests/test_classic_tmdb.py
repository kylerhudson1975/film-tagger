"""Classic Porn people: TMDB is the primary source for photo + profile, ThePornDB only fills what TMDB lacks; scenes-only
people keep ThePornDB primary; mainstream people stay TMDB. Confident TMDB match (film credits, else name/alias search with
known_for overlap), ambiguous -> listed for review. Backfill skips hand edits / custom photos. TMDB mocked (no network).
Run: python tests/test_classic_tmdb.py"""
import os, sys, json, tempfile, time, io, urllib.request, urllib.parse
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
def film(t, y, cast, tmdb=None, pg=None, kind=None, path=None):
    fid = c.execute("INSERT INTO films(title,year,cast_,primary_genre,tmdb_id,kind) VALUES(?,?,?,?,?,?)", (t, y, cast, pg, tmdb, kind)).lastrowid
    if path: c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,1)", (fid, path))
    return fid
film("Classic A", 1978, "Annie Test, Nophoto Test, Dup Test, Nobody Test, Edited Test, Cropped Test, Already Test", 500, path="/lib/Classic Porn/A (1978)/a.mkv")
film("Classic B", 1980, "Alias Real", 501, pg="Hardcore")          # genre-based Classic Porn (no folder)
film("Scene 1", 2015, "Sara Scene", kind="scene", path="/lib/Scenes/s1.mp4")
film("Drama", 1990, "Main Test", 600, pg="Drama", path="/lib/Films/d.mkv")
db.set_setting(c, "adult_roots", ["/lib/Classic Porn"]); db.set_setting(c, "tmdb_key", "k" * 32)
def person(name, **kw):
    kw.setdefault("source", "tpdb"); kw.setdefault("tpdb_performer_id", "55"); kw.setdefault("tpdb_fetched_at", time.time())
    kw.setdefault("image_url", f"https://cdn.tpdb.test/{name.split()[0].lower()}.jpg")
    cols = ["name", "name_norm"] + list(kw)
    return c.execute(f"INSERT INTO people({','.join(cols)}) VALUES({','.join('?'*len(cols))})", [name, db.fold(name)] + list(kw.values())).lastrowid
annie = person("Annie Test", bio="tpdb bio", nationality="American", birthday="1950-01-01", birth_date_source="theporndb 55")
noph = person("Nophoto Test", bio="tpdb bio 2")
dup = person("Dup Test"); nob = person("Nobody Test")
edited = person("Edited Test", bio="my words", edited_fields='["bio"]')
crop = person("Cropped Test", photo_file="crop-1.jpg", photo_orig="orig-1.jpg")
already = person("Already Test", source="tmdb", tmdb_person_id=950, profile_path="/already.jpg")
alias = person("Alias Stage", aliases='["Alias Real"]')
sara = person("Sara Scene", profile_path="/sara-tmdb.jpg")
main = c.execute("INSERT INTO people(name,name_norm,source,tmdb_person_id,profile_path,credits_json,fetched_at) VALUES('Main Test','main test','tmdb',960,'/main.jpg','{}',?)", (time.time(),)).lastrowid
c.commit()

CREDITS = {500: ["Annie Test:900", "Nophoto Test:901", "Already Test:950"], 501: []}
SEARCH = {"Dup Test": [dict(id=910, name="Dup Test", known_for=[dict(id=1, title="Other")]), dict(id=911, name="Dup Test", known_for=[dict(id=2, title="Else")])],
          "Alias Real": [dict(id=920, name="Alias Real", known_for=[dict(id=501, title="Classic B")]), dict(id=921, name="Alias Real", known_for=[])],
          "Alias Stage": []}
PERSON = {900: dict(profile_path="/annie.jpg", biography="tmdb bio", birthday="1951-02-02", place_of_birth="Paris", deathday=None),
          901: dict(profile_path=None, biography="", birthday=None, place_of_birth=None),
          920: dict(profile_path="/alias.jpg", biography="alias bio", birthday="1955-05-05", place_of_birth="Rome"),
          950: dict(profile_path="/already.jpg", biography="b", birthday="1949-01-01")}
calls = []
real = urllib.request.urlopen
def fake(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if "api.themoviedb.org" not in url: raise AssertionError("no network in tests: " + url)
    calls.append(url); u = urllib.parse.urlparse(url); q = urllib.parse.parse_qs(u.query)
    if "/movie/" in u.path:
        t = int(u.path.split("/movie/")[1].split("/")[0])
        body = {"cast": [dict(name=x.split(":")[0], id=int(x.split(":")[1])) for x in CREDITS.get(t, [])], "crew": []}
    elif u.path.endswith("/search/person"): body = {"results": SEARCH.get(q["query"][0], [])}
    else:
        pid = int(u.path.rsplit("/", 1)[1]); body = dict(PERSON[pid], id=pid, combined_credits={"cast": [], "crew": []}, known_for_department="Acting")
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return R(json.dumps(body).encode())
urllib.request.urlopen = fake

# unit: photo choice + fill-only ThePornDB
row = lambda i: c.execute("SELECT * FROM people WHERE id=?", (i,)).fetchone()
assert people.photo_choice(row(sara)) == ("tpdb", "https://cdn.tpdb.test/sara.jpg")       # scenes-only: ThePornDB first
assert people.photo_choice(row(main)) == ("tmdb", "/main.jpg")
assert people.classic_film_ids(c) == {1, 2}
r0 = dict(row(nob)); r0.update(source="tmdb", profile_path="/x.jpg", bio="T", nationality=None)
people.apply_tpdb(c, r0, dict(id=77, bio="TP", extras=dict(nationality="French"), image="https://cdn.tpdb.test/n2.jpg"), fill_only=True)
r1 = row(nob); assert r1["nationality"] == "French" and r1["bio"] is None and r1["source"] == "tpdb"   # source column untouched by fill-only (row was tpdb)
c.execute("UPDATE people SET nationality=NULL, tpdb_performer_id='55' WHERE id=?", (nob,)); c.commit()

cl = app.app.test_client()
st = cl.post("/api/people/classic_backfill", json={"delay": 0}).json
for _ in range(100):
    time.sleep(0.1); st = cl.get("/api/people/classic_backfill").json
    if not st["running"]: break
n = st["counts"]
assert st["total"] == 8, st      # Annie, Nophoto, Dup, Nobody, Edited, Cropped, Already, Alias (not Sara, not Main)
assert n["switched_to_tmdb_photo"] == 2 and n["already_tmdb"] == 1 and n["tmdb_no_photo_kept_tpdb"] == 1, n
assert n["skipped_hand_edits"] == 2 and n["no_tmdb_match"] == 1 and n["ambiguous"] == 1 and n["errors"] == 0, n
assert [x["name"] for x in st["review"]] == ["Dup Test"] and len(st["review"][0]["candidates"]) == 2
a = row(annie)
assert (a["source"], a["tmdb_person_id"], a["profile_path"], a["bio"], a["birthday"], a["place"]) == ("tmdb", 900, "/annie.jpg", "tmdb bio", "1951-02-02", "Paris"), dict(a)
assert a["nationality"] == "American" and a["image_url"] and a["birth_date_source"] == "tmdb person 900"   # ThePornDB fills what TMDB lacks; old image kept
assert people.photo_choice(a) == ("tmdb", "/annie.jpg")
p2 = row(noph); assert p2["tmdb_person_id"] == 901 and p2["bio"] == "tpdb bio 2" and people.photo_choice(p2)[0] == "tpdb"
assert row(alias)["tmdb_person_id"] == 920 and row(alias)["profile_path"] == "/alias.jpg"   # alias search + known_for overlap
assert row(dup)["tmdb_person_id"] is None and row(nob)["tmdb_person_id"] == people.NO_MATCH
assert row(edited)["bio"] == "my words" and row(edited)["source"] == "tpdb" and row(crop)["photo_file"] == "crop-1.jpg"
assert row(sara)["source"] == "tpdb" and row(main)["profile_path"] == "/main.jpg"
# person page Source line
pp = cl.get(f"/api/person/{annie}").json["person"]
assert pp["source"] == "TMDB + ThePornDB for details TMDB lacks" and pp["classic"], pp["source"]
assert "/api/person/" in (pp.get("photo") or "")
pp = cl.get(f"/api/person/{dup}").json["person"]
assert pp["source"] == "ThePornDB (no TMDB person found yet)", pp["source"]
pp = cl.get(f"/api/person/{sara}").json["person"]
assert pp["source"].startswith("ThePornDB") and not pp["classic"], pp["source"]
# page load of a not-yet-switched Classic Porn person switches them (same rule as the backfill)
c.execute("UPDATE people SET source='tpdb', credits_json='{}', fetched_at=? WHERE id=?", (time.time(), annie)); c.commit()
cl.get(f"/api/person/{annie}"); assert row(annie)["source"] == "tmdb"
print("OK test_classic_tmdb")
