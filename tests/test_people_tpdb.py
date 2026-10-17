import os, sys, json, sqlite3, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import people as P
c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
c.executescript(P.PEOPLE_SCHEMA); P.migrate(c); P.migrate(c)
c.execute("INSERT INTO people(name,name_norm) VALUES('Gigi Dior','gigi dior')"); c.commit()
row = lambda: c.execute("SELECT * FROM people WHERE id=1").fetchone()
perf = {"id": "abc-1", "name": "Gigi Dior", "bio": "Bio text", "aliases": ["G. Dior"], "image": "https://cdn.example/i.jpg",
        "extras": {"birthday": "1990-05-01", "birthplace": "Ohio, USA", "nationality": "American", "measurements": "34D-26-36",
                   "height": "165cm", "career_start_year": 2016, "links": {"IAFD": "https://www.iafd.com/x", "Twitter": "https://x.com/g", "Bad": None}}}
calls = []
def call(path, **kw):
    calls.append((path, kw))
    if path == "/performers": return {"data": [{"id": "zzz", "name": "Gigi D"}, {"id": "abc-1", "name": "gigi dior"}]}
    return {"data": perf}
assert P.tpdb_update(c, "t", row(), call) == "found"
r = row()
assert r["source"] == "tpdb" and r["tpdb_performer_id"] == "abc-1" and r["career"] == "2016–present"
assert json.loads(r["links_json"]) == {"IAFD": "https://www.iafd.com/x", "Twitter": "https://x.com/g"}
assert r["image_url"] and json.loads(r["aliases"]) == ["G. Dior"]
# hand edits survive
c.execute("UPDATE people SET bio='Mine', edited_fields='[\"bio\"]' WHERE id=1"); c.commit()
perf["bio"] = "New"; P.tpdb_update(c, "t", row(), call); assert row()["bio"] == "Mine"
# no IAFD invented when TPDB has none
perf["extras"]["links"] = {}; P.tpdb_update(c, "t", row(), call); assert row()["links_json"] is None
# image suppressed for review-flagged people
c.execute("UPDATE people SET image_url=NULL"); P.tpdb_update(c, "t", row(), call, allow_image=False); assert row()["image_url"] is None
# not found
c.execute("INSERT INTO people(name,name_norm) VALUES('Nobody X','nobody x')"); c.commit()
r2 = c.execute("SELECT * FROM people WHERE id=2").fetchone()
assert P.tpdb_update(c, "t", r2, lambda p, **k: {"data": []}) == "not_found"
assert c.execute("SELECT tpdb_performer_id FROM people WHERE id=2").fetchone()[0] == P.TPDB_NONE
# alias pick + id parsing
assert P.pick_performer([{"id": 1, "name": "A", "aliases": ["Nobody X"]}], "nobody x")["id"] == 1
assert P.parse_tpdb_performer("https://theporndb.net/performers/gigi-dior") == "gigi-dior"
assert P.parse_tpdb_performer("abc-1") == "abc-1" and P.parse_tpdb_performer("a b") is None
print("ok")
