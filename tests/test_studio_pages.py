"""Studio pages and the Studios list: in-library counts equal the library list (aliases, several studios per title,
scene <Studio> folders, library folder ?lib=), country = hand edit > TMDB origin country > inferred from the titles'
production countries, TMDB company / ThePornDB site lookups cached (HTTP mocked), hand edits survive lookups, and
performers follow the age rule. Temp DB, no network. Run: python tests/test_studio_pages.py"""
import os, sys, tempfile, time, urllib.parse, urllib.error
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, studios as S
S.TMDB_GAP = 0
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
tmp = tempfile.mkdtemp(); movies = os.path.join(tmp, "Movies"); classic = os.path.join(tmp, "Classic"); scenes = os.path.join(tmp, "Scenes")
db.set_setting(c, "roots", [movies, classic]); db.set_setting(c, "scene_roots", [scenes])
db.set_setting(c, "tmdb_key", "k"); db.set_setting(c, "tpdb_key", "t")
def film(t, y, st, country="", path=None, kind=None, tmdb_id=None, director="", cast="", pg="", rd=None, tpdb_id=None):
    i = c.execute("INSERT INTO films(title,year,studio,country,kind,tmdb_id,director,cast_,primary_genre,release_date,tpdb_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (t, y, st, country, kind, tmdb_id, director, cast, pg, rd, tpdb_id)).lastrowid
    if path: c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,?)", (i, path, 1))
    return i
film("Flossie", 1974, "Horn Produksjon", "Sweden", os.path.join(classic, "Flossie.mkv"), tmdb_id=96157, director="Mac Ahlberg", cast="Maria Lynn, Nobody Known", pg="Hardcore", rd="1974-03-01")
film("Justine & Juliette", 1975, "Horn Produksjon, Filminvest AB", "Sweden", os.path.join(classic, "JJ.mkv"), tmdb_id=1, director="Mac Ahlberg", cast="Maria Lynn", pg="Hardcore", rd="1975-05-01")
film("Molly", 1977, "horn produksjon", "Sweden, Denmark", os.path.join(classic, "Molly.mkv"), director="Mac Ahlberg", pg="Hardcore")
film("Second Coming of Eva", 1974, "Filminvest", "Sweden", os.path.join(classic, "Eva.mkv"), tmdb_id=200568, pg="Hardcore")  # alias below
film("Cathy", 1977, "Alpha France", "France", os.path.join(classic, "Cathy.mkv"), tmdb_id=168411, director="Burd Tranbaree", pg="Hardcore")
film("Big Movie", 1990, "Warner Bros.", "United States of America", os.path.join(movies, "Big.mkv"), tmdb_id=5, director="Someone", pg="Drama")
sc = film("Scene A", 2020, "", path=os.path.join(scenes, "Studio Z", "a.mp4"), kind="scene", tpdb_id="abc")
film("Scene B", 2021, "Studio Z", path=os.path.join(scenes, "Studio Z", "b.mp4"), kind="scene")
c.commit()
db.add_studio_alias(c, "Filminvest", "Filminvest AB")
lynn = c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Maria Lynn',?, '1950-01-01')", (db.fold("Maria Lynn"),)); c.commit()
cl = app.app.test_client()
def listed(**q): return cl.get("/api/films?limit=1&" + urllib.parse.urlencode(q)).json["total"]

# ---- list: counts equal the library list; aliases fold; folder filter ----
L = cl.get("/api/studios/list").json
byname = {s["name"]: s for s in L["studios"]}
assert set(byname) == {"Horn Produksjon", "Filminvest AB", "Alpha France", "Warner Bros.", "Studio Z"}, set(byname)
for s in L["studios"]: assert s["n"] == listed(studio=s["name"]), s
assert byname["Horn Produksjon"]["n"] == 3 and byname["Filminvest AB"]["n"] == 2 and byname["Studio Z"]["n"] == 2
assert (byname["Horn Produksjon"]["year_min"], byname["Horn Produksjon"]["year_max"]) == (1974, 1977)
assert byname["Horn Produksjon"]["country"] == "Sweden" and byname["Horn Produksjon"]["country_src"] == "inferred"
assert byname["Studio Z"]["country"] is None and byname["Studio Z"]["scenes"] == 2
Lm = cl.get("/api/studios/list", query_string={"lib": movies}).json
assert [s["name"] for s in Lm["studios"]] == ["Warner Bros."] and Lm["studios"][0]["n"] == listed(studio="Warner Bros.", lib=movies) == 1
Ls = cl.get("/api/studios/list", query_string={"lib": os.path.join(scenes, "Studio Z")}).json
assert [(s["name"], s["n"]) for s in Ls["studios"]] == [("Studio Z", 2)]

# ---- TMDB lookups (mocked) ----
calls = []
def fake_tmdb(path, key, **p):
    calls.append(path)
    if path == "movie/96157": return {"production_companies": [{"id": 80631, "name": "Horn Produksjon"}]}
    if path.startswith("movie/"): return {"production_companies": []}
    if path == "company/80631": return {"name": "Horn Produksjon", "origin_country": "NO", "headquarters": "Oslo, Norway", "parent_company": None, "logo_path": None, "description": ""}
    if path == "discover/movie":
        assert p["with_companies"] == 80631 and p["include_adult"] == "true"
        return {"total_results": 4, "total_pages": 1, "results": [
            {"id": 96157, "title": "Flossie", "release_date": "1974-03-01"}, {"id": 2, "title": "Not Owned", "release_date": "1976-01-01"},
            {"id": 3, "title": "Also Not", "release_date": "1977-01-01"}, {"id": 4, "title": "Fourth", "release_date": ""}]}
    if path == "search/company": return {"results": []}
    if path == "configuration/countries": return [{"iso_3166_1": "NO", "english_name": "Norway"}, {"iso_3166_1": "SE", "english_name": "Sweden"}]
    raise urllib.error.URLError("offline")
S.TMDB["fn"] = fake_tmdb
def page(name, **q):
    for _ in range(200):
        d = cl.get("/api/studio", query_string=dict(name=name, **q)).json
        if not d.get("loading"): return d
        q.pop("refresh", None); time.sleep(0.05)
    raise AssertionError("still loading")
cl.get("/api/studio", query_string={"name": "Horn Produksjon"}); time.sleep(0.05)
d = page("Horn Produksjon")
assert d["name"] == "Horn Produksjon" and d["in_library"] == 3 == listed(studio="Horn Produksjon") == len(d["titles"])
assert d["country"] == "Norway" and d["country_src"] == "tmdb" and d["iso"] == "NO", (d["country"], d["country_src"])
assert d["info"]["tmdb_company_id"] == 80631 and d["info"]["headquarters"] == "Oslo, Norway" and d["total"] == 4
assert d["catalog"]["total"] == 4 and d["catalog"]["owned"] == 1 and [x["film_id"] is not None for x in d["catalog"]["items"]] == [True, False, False, False]
assert d["years"] == [1974, 1977] and d["stats"]["directors"][0] == {"name": "Mac Ahlberg", "n": 3}
# age rule: 'Nobody Known' has no birth date -> left out of regular performers on adult titles
assert [x["name"] for x in d["stats"]["cast"]] == ["Maria Lynn"] and d["stats"]["cast_left_out"] == 1
# hand edit wins over TMDB and survives a refresh
assert cl.post("/api/studio/edit", json={"name": "horn produksjon", "country": "Sweden"}).json["ok"]
d = page("Horn Produksjon", refresh=1)
assert d["country"] == "Sweden" and d["country_src"] == "hand" and d["iso"] == "SE" and "country" in d["info"]["edited"]
L = cl.get("/api/studios/list").json; h = [s for s in L["studios"] if s["name"] == "Horn Produksjon"][0]
assert h["country"] == "Sweden" and h["country_src"] == "hand" and h["total"] == 4 and h["n"] == 3
assert L["countries"][:2] == [{"name": "Sweden", "iso": "SE", "n": 2}, {"name": "France", "iso": "FR", "n": 1}], L["countries"]
assert cl.post("/api/studio/edit", json={"name": "Horn Produksjon", "country": ""}).json["ok"]
assert page("Horn Produksjon")["country"] == "Norway"
# alias spelling opens the main studio
d = page("Filminvest")
assert d["name"] == "Filminvest AB" and d["aliases"] == ["Filminvest"] and d["in_library"] == 2 == listed(studio="Filminvest AB")
assert d["country"] == "Sweden" and d["country_src"] == "inferred"   # TMDB had no company -> inferred
# folder scope on the page
assert page("Horn Produksjon", lib=movies)["in_library"] == 0
# ---- offline: fails gracefully ----
S.TMDB["fn"] = lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("offline"))
S.PAGEJOBS.clear()
d = page("Warner Bros.")
assert d["in_library"] == 1 and d["catalog"] is None and "offline" in (d["error"] or ""), d["error"]
# ---- ThePornDB site for scene studios (mocked) ----
def fake_tpdb(path, token, **p):
    if path == "/sites": return {"data": [{"id": 77, "name": "Studio Z", "short_name": "studioz", "url": "https://z.example", "logo": None, "parent": {"name": "Net Z"}}]}
    if path == "/scenes" and p.get("per_page") == 1: return {"meta": {"total": 120}}
    if path == "/scenes": return {"meta": {"total": 120, "last_page": 1}, "data": [{"id": "abc", "title": "Scene A", "date": "2020-01-01"}, {"id": "zzz", "title": "Other", "date": "2019-01-01"}]}
    raise AssertionError(path)
S.tpdbmod.call_retry = fake_tpdb
d = page("Studio Z")
assert d["scene_only"] and d["total"] == 120 and d["total_src"] == "tpdb" and d["info"]["parent"] == "Net Z"
assert d["in_library"] == 2 and d["catalog"]["owned"] == 1
# ---- background job: resumable (skips studios already checked) ----
S.TMDB["fn"] = fake_tmdb
st = cl.post("/api/studios/backfill", json={}).json; assert st["started"]
for _ in range(200):
    j = cl.get("/api/studios/backfill").json
    if not j["job"]["running"]: break
    time.sleep(0.05)
assert j["checked"] == j["studios"] == 5, j
assert cl.post("/api/studios/backfill", json={}).json["started"]
for _ in range(200):
    j = cl.get("/api/studios/backfill").json
    if not j["job"]["running"]: break
    time.sleep(0.05)
assert j["job"]["total"] == 0, j   # nothing left to look up
print("OK test_studio_pages")
