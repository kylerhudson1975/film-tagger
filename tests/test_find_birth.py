"""Find birth date (/api/people/<id>/find_birth): candidates from ThePornDB (linked id), TMDB (person id) and Wikidata P569
(year-only precision kept as year only), each checked against the library titles (under 18 at a title = impossible), sources
that disagree flagged, nothing saved. Network mocked. Run: python tests/test_find_birth.py"""
import os, sys, io, json, tempfile, urllib.request, urllib.parse
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); _c0.commit()
import app, tpdb, birthdates as bd
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tmdb_key", "k" * 32); db.set_setting(c, "tpdb_key", "t")
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(1,'Early One','scene','Ann Old','1978-05-01',1978)")
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(2,'Later','scene','Ann Old','1985-01-01',1985)")
for i in (1, 2): c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\{i}.mp4"))
c.commit(); cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Ann Old").json["id"]
c.execute("UPDATE people SET tpdb_performer_id='p9', tmdb_person_id=55 WHERE id=?", (pid,)); c.commit()
tpdb.call_retry = lambda path, tok, **k: {"data": {"id": "p9", "slug": "ann-old", "name": "Ann Old", "extras": {"birthday": "1960-03-01"}}}
def fake(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    assert "api.themoviedb.org" in url, "no network: " + url
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return R(json.dumps({"id": 55, "name": "Ann Old", "birthday": "1962-01-01"}).encode())
urllib.request.urlopen = fake
def wd(url, params=None, **k):
    if params and params.get("action") == "wbsearchentities": return {"search": [{"id": "Q1"}]} if params["search"] == "Ann Old" else {"search": []}
    if params and params.get("action") == "wbgetentities":
        return {"entities": {"Q1": {"id": "Q1", "labels": {"en": {"value": "Ann Old"}}, "descriptions": {"en": {"value": "actress"}},
                "claims": {"P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q5"}}}}],
                           "P569": [{"rank": "normal", "mainsnak": {"datavalue": {"value": {"time": "+1959-00-00T00:00:00Z", "precision": 9}}}}]}}}}
    raise AssertionError("unexpected " + url)
bd._get = wd
before = c.execute("SELECT birthday, edited_fields FROM people WHERE id=?", (pid,)).fetchone()[:]
r = cl.get(f"/api/people/{pid}/find_birth").json
cs = {x["source"]: x for x in r["candidates"]}
assert set(cs) == {"ThePornDB", "TMDB", "Wikidata"}, r
assert cs["ThePornDB"]["date"] == "1960-03-01" and not cs["ThePornDB"]["impossible"] and cs["ThePornDB"]["age_first"] == 18
assert cs["ThePornDB"]["url"] == "https://theporndb.net/performers/ann-old"
assert cs["TMDB"]["impossible"] and cs["TMDB"]["age_first"] == 16 and "Early One" in cs["TMDB"]["why"]
assert cs["Wikidata"]["year_only"] and cs["Wikidata"]["date"] is None and cs["Wikidata"]["year"] == 1959 and not cs["Wikidata"]["impossible"]
assert r["conflict"] and all(x["conflict"] for x in r["candidates"]) and r["titles"] == 2 and r["first_title"]["title"] == "Early One"
assert c.execute("SELECT birthday, edited_fields FROM people WHERE id=?", (pid,)).fetchone()[:] == before  # nothing saved
# Accept = the normal edit path: protected, source recorded
cl.post(f"/api/person/{pid}/edit", json={"birthday": "1960-03-01", "birth_date_note": "ThePornDB https://theporndb.net/performers/ann-old"})
row = c.execute("SELECT birthday, birth_date_source, edited_fields FROM people WHERE id=?", (pid,)).fetchone()
assert row[0] == "1960-03-01" and "ThePornDB" in row[1] and "birthday" in json.loads(row[2])
assert cl.get(f"/api/people/{pid}/summary").json["birthday"] == "1960-03-01"
print("ok")
