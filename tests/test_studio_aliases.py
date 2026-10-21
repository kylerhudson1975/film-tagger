"""Studio aliases: merge, canonical name on every write path, search, ThePornDB scoring. Run: python tests/test_studio_aliases.py"""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
cl = app.app.test_client()
c = db.connect()
for t, st in (("A", "Angels Love"), ("B", "angels.love"), ("C", "angels.love"), ("D", "Other Studio"), ("E", "Dup, Dup")):
    c.execute("INSERT INTO films(title,kind,studio,updated_at) VALUES(?, 'scene', ?, 1)", (t, st))
c.commit()
assert db.studio_key("angels.love") == db.studio_key("ANGELS-Love") == "angelslove"
assert cl.post("/api/studio-aliases", json={"alias": "Angels Love", "canonical": "Angels Love"}).status_code == 400
r = cl.post("/api/studio-aliases", json={"alias": "angels.love", "canonical": "Angels Love"}).json
assert r["updated"] == 2, r
rows = {x["title"]: (x["studio"], x["updated_at"]) for x in db.connect().execute("SELECT title, studio, updated_at FROM films")}
assert rows["B"][0] == rows["C"][0] == "Angels Love" and isinstance(rows["B"][1], float) and rows["B"][1] > 1e9
assert rows["A"][1] == 1 and rows["E"] == ("Dup, Dup", 1)  # unrelated rows untouched
assert cl.get("/api/studio-aliases").json["aliases"] == [{"alias": "angels.love", "canonical": "Angels Love"}]
assert [x["name"] for x in cl.get("/api/studios").json] == ["Angels Love", "Dup", "Other Studio"]
ids = lambda q: sorted(f["title"] for f in cl.get("/api/films?" + q).json["films"])
assert ids("studio=angels.love") == ["A", "B", "C"]
assert ids("q=angels.love&in=studio") == ["A", "B", "C"]
assert ids("q=angels.love") == ["A", "B", "C"]
# manual edit, bulk edit, ThePornDB apply, scan all store the main name
fid = [x["id"] for x in cl.get("/api/films?studio=Other Studio").json["films"]][0]
assert cl.put(f"/api/films/{fid}", json={"studio": "ANGELS.LOVE"}).status_code == 200
assert db.connect().execute("SELECT studio FROM films WHERE id=?", (fid,)).fetchone()[0] == "Angels Love"
cl.post("/api/bulk/studio", json={"film_ids": [fid], "studio": "angels.love"})
assert db.connect().execute("SELECT studio FROM films WHERE id=?", (fid,)).fetchone()[0] == "Angels Love"
c = db.connect(); app.apply_scene(c, fid, {"id": 1, "title": "T", "site": {"name": "angels.love"}}); c.commit()
r = c.execute("SELECT studio, updated_at FROM films WHERE id=?", (fid,)).fetchone()
assert r[0] == "Angels Love" and isinstance(r[1], float)
assert db.canon_studio("angels.love, Other Studio") == "Angels Love, Other Studio"
assert db.canon_studio("X, Y, X") == "X, Y, X"  # values without an alias are never rewritten
# ThePornDB: the alias counts as the same studio
tpdb._STUDIO_CACHE.clear()
assert "studio" in tpdb.score({"site": {"name": "angels.love"}, "title": "x"}, "Angels Love", None, "x")[1]
assert tpdb.resolve_site(lambda p, **q: {"data": [{"name": "angels.love"}]} if q.get("q") == "angels.love" else {"data": []}, "Angels Love") == ["angels.love"]
# remove
cl.delete("/api/studio-aliases?alias=angels.love")
assert cl.get("/api/studio-aliases").json["aliases"] == [] and db.canon_studio("angels.love") == "angels.love"
print("ok")
