"""Performer aliases: search, person page, manual aliases, merge, ThePornDB scoring. Run: python tests/test_person_aliases.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
cl = app.app.test_client()
c = db.connect()
for t, cast in (("Scene A", "Ann Example, Bob Other"), ("Scene B", "Annie Sample"), ("Scene C", "Cara Third"), ("Scene D", "Dee Fourth")):
    c.execute("INSERT INTO films(title,kind,cast_) VALUES(?, 'scene', ?)", (t, cast))
c.commit()
ids = lambda q: sorted(f["title"] for f in cl.get("/api/films?" + q).json["films"])
pa = cl.get("/api/person/lookup?name=Ann Example").json["id"]
# 1. no aliases yet: plain search
assert ids("q=Annie Sample&in=cast") == ["Scene B"]
# 3. manual alias, protected from refresh
cl.post(f"/api/person/{pa}/edit", json={"aliases": "Annie Sample, Ann"})
p = cl.get(f"/api/person/{pa}").json["person"]
assert p["aliases"] == ["Annie Sample", "Ann"] and "aliases" in p["edited"]
row = db.connect().execute("SELECT * FROM people WHERE id=?", (pa,)).fetchone()
import people; people.apply_tpdb(db.connect(), row, {"id": "x", "name": "Ann Example", "aliases": ["Other Name"]})
assert cl.get(f"/api/person/{pa}").json["person"]["aliases"] == ["Annie Sample", "Ann"]
# 1. library search by either name finds both scenes (single-word alias 'Ann' is not used for linking)
assert ids("q=Annie Sample&in=cast") == ["Scene A", "Scene B"]
assert ids("q=ann example&in=cast") == ["Scene A", "Scene B"]
assert ids("q=annie sample") == ["Scene A", "Scene B"]
assert ids("person=Annie Sample&prole=cast") == ["Scene A", "Scene B"]
assert cl.get("/api/people?name=Ann Example").json["counts"]["cast"] == 2
# 2. person page includes scenes credited under the alias
lib = cl.get(f"/api/person/{pa}").json["library"]
assert sorted(f["title"] for f in lib) == ["Scene A", "Scene B"]
# people search by alias finds the person
assert any(x["pid"] == pa for x in cl.get("/api/people/search?q=annie sample").json["people"])
# 4. merge Dee Fourth into Cara Third
pc = cl.get("/api/person/lookup?name=Cara Third").json["id"]
pd = cl.get("/api/person/lookup?name=Dee Fourth").json["id"]
assert cl.post(f"/api/person/{pc}/merge", json={"other": pc}).status_code == 400
r = cl.post(f"/api/person/{pc}/merge", json={"other": pd}).json
assert r["ok"] and "Dee Fourth" in r["aliases"]
pg = cl.get(f"/api/person/{pc}").json
assert sorted(f["title"] for f in pg["library"]) == ["Scene C", "Scene D"] and pg["person"]["merged"][0]["name"] == "Dee Fourth"
assert cl.get(f"/api/person/{pd}").json["person"]["id"] == pc  # the merged person's page shows the target
assert ids("q=dee fourth&in=cast") == ["Scene C", "Scene D"]
assert [x["pid"] for x in cl.get("/api/people/search?q=dee fourth").json["people"]] == [pc]
cl.post(f"/api/person/{pd}/unmerge", json={})
assert cl.get(f"/api/person/{pd}").json["person"]["id"] == pd
# 5. ThePornDB scoring: a performer credited under an alias counts as a performer match
tpdb._ALIAS_CACHE["at"] = 0
sc = {"title": "Something", "performers": [{"name": "Ann Example"}], "site": {"name": "Zed"}}
assert "performers 1/1" in tpdb.score(sc, None, None, "annie sample something else")[1]
sc2 = {"title": "Something", "performers": [{"name": "Xy Zzzzz", "parent": {"aliases": ["Long Alias Name"]}}], "site": {}}
assert any(w.startswith("performers") for w in tpdb.score(sc2, None, None, "long alias name x")[1])
assert not any(w.startswith("performers") for w in tpdb.score(sc2, None, None, "nobody here")[1])
print("ok")
