"""Primary-name rename: aliases, credit rewrite, lookup, conflict with Merge. Run: python tests/test_person_rename.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people
cl = app.app.test_client()
c = db.connect()
c.execute("INSERT INTO films(title,kind,cast_,director,cast_roles) VALUES('Film A','movie','Ann Example, Bob Other','Cara Third',?)",
          (json.dumps({"Ann Example": "The Lead", "Bob Other": "Friend"}),))
c.execute("INSERT INTO films(title,kind,cast_) VALUES('Film B','movie','Annie Sample')")
c.execute("INSERT INTO films(title,kind,cast_) VALUES('Film C','movie','Dee Fourth')")
c.commit()

pa = cl.get("/api/person/lookup?name=Ann Example").json["id"]
pb = cl.get("/api/person/lookup?name=Bob Other").json["id"]
pc = cl.get("/api/person/lookup?name=Cara Third").json["id"]
pd = cl.get("/api/person/lookup?name=Dee Fourth").json["id"]
cl.post(f"/api/person/{pa}/edit", json={"aliases": "Annie Sample, Ann Ex"})
# gallery / ids stay put
c = db.connect()
c.execute("UPDATE people SET gallery_dir=?, tpdb_performer_id=?, tmdb_person_id=?, photo_file=? WHERE id=?",
          ("C:\\Pics\\Ann", "tpdb-ann", 5547295, "manual-ann.jpg", pa))
c.commit()

# empty name
r = cl.post(f"/api/person/{pa}/rename", json={"name": "  "})
assert r.status_code == 400 and "required" in r.json["error"].lower()

# conflict: another person's primary name
r = cl.post(f"/api/person/{pa}/rename", json={"name": "Bob Other"})
assert r.status_code == 400 and "already exists" in r.json["error"] and "Merge" in r.json["error"]
assert cl.get(f"/api/person/{pa}").json["person"]["name"] == "Ann Example"

# conflict: another person's alias
cl.post(f"/api/person/{pc}/edit", json={"aliases": "Cara Alias Name"})
r = cl.post(f"/api/person/{pa}/rename", json={"name": "Cara Alias Name"})
assert r.status_code == 400 and "alias of Cara Third" in r.json["error"] and "Merge" in r.json["error"]

# same name is a no-op
r = cl.post(f"/api/person/{pa}/rename", json={"name": "Ann Example"}).json
assert r["ok"] and r.get("unchanged") and r["credits_updated"] == 0

# rename via /rename: old name becomes alias, existing alias kept, new name dropped from aliases
r = cl.post(f"/api/person/{pa}/rename", json={"name": "Ann Ex"}).json
assert r["ok"] and r["name"] == "Ann Ex" and r["credits_updated"] >= 1
assert "Ann Example" in r["aliases"] and "Annie Sample" in r["aliases"] and "Ann Ex" not in r["aliases"]

p = cl.get(f"/api/person/{pa}").json
assert p["person"]["name"] == "Ann Ex"
assert "Ann Example" in p["person"]["aliases"] and "Annie Sample" in p["person"]["aliases"]
assert "name" in p["person"]["edited"] and "aliases" in p["person"]["edited"]
# library credits follow and still link (including the single-word-ish former alias Annie Sample — two words)
lib = sorted(f["title"] for f in p["library"])
assert lib == ["Film A", "Film B"]
# film rows rewritten (not the other people's names)
c = db.connect()
rows = {r["title"]: r for r in c.execute("SELECT title, cast_, director, cast_roles FROM films")}
assert rows["Film A"]["cast_"] == "Ann Ex, Bob Other"
assert rows["Film A"]["director"] == "Cara Third"
assert rows["Film B"]["cast_"] == "Annie Sample"
assert json.loads(rows["Film A"]["cast_roles"]) == {"Ann Ex": "The Lead", "Bob Other": "Friend"}
# photo / gallery / ids untouched
row = c.execute("SELECT gallery_dir, tpdb_performer_id, tmdb_person_id, photo_file, name_norm FROM people WHERE id=?", (pa,)).fetchone()
assert row["gallery_dir"] == "C:\\Pics\\Ann" and row["tpdb_performer_id"] == "tpdb-ann"
assert row["tmdb_person_id"] == 5547295 and row["photo_file"] == "manual-ann.jpg"
assert row["name_norm"] == "ann ex"

# lookup by old primary and by remaining alias finds the same person (does not create a duplicate)
assert cl.get("/api/person/lookup?name=Ann Example").json["id"] == pa
assert cl.get("/api/person/lookup?name=Annie Sample").json["id"] == pa
assert cl.get("/api/person/lookup?name=Ann Ex").json["id"] == pa
assert cl.get("/api/person/lookup?name=ann example").json["name"] == "Ann Ex"
# no stray row for the old name
assert c.execute("SELECT COUNT(*) FROM people WHERE name_norm='ann example'").fetchone()[0] == 0

# people search by old or new name finds her
assert any(x["pid"] == pa and x["name"] == "Ann Ex" for x in cl.get("/api/people/search?q=Ann Example").json["people"])
assert any(x["pid"] == pa for x in cl.get("/api/people/search?q=Ann Ex").json["people"])

# edit endpoint also renames, and can be a spelling-only change (no self-alias)
r = cl.post(f"/api/person/{pa}/edit", json={"name": "Ann Extra", "career": "1977–1980"}).json
assert r["ok"]
p = cl.get(f"/api/person/{pa}").json["person"]
assert p["name"] == "Ann Extra" and p["career"] == "1977–1980"
assert "Ann Ex" in p["aliases"] and "Ann Example" in p["aliases"]
assert "Ann Extra" not in p["aliases"]
# spelling-only: fold stays the same, old exact spelling is not added as an alias of itself
r = cl.post(f"/api/person/{pa}/rename", json={"name": "ANN EXTRA"}).json
assert r["ok"] and r["name"] == "ANN EXTRA"
p = cl.get(f"/api/person/{pa}").json["person"]
assert p["aliases"].count("Ann Extra") == 0 and "ANN EXTRA" not in p["aliases"]
c = db.connect()
assert c.execute("SELECT name, name_norm FROM people WHERE id=?", (pa,)).fetchone()["name_norm"] == "ann extra"
assert "ANN EXTRA" in c.execute("SELECT cast_ FROM films WHERE title='Film A'").fetchone()[0]

# unknown person
assert cl.post("/api/person/999999/rename", json={"name": "X"}).status_code == 404

# Bob's credits and page unchanged
assert cl.get(f"/api/person/{pb}").json["person"]["name"] == "Bob Other"
assert any(f["title"] == "Film A" for f in cl.get(f"/api/person/{pb}").json["library"])

print("ok")
