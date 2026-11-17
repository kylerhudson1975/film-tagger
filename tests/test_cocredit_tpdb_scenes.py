"""Credited with: a typed co-performer on a cached ThePornDB scene is found even when that scene is not in the library."""
import os, sys, json, tempfile
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
# One library scene (keeps existing library results) and two ThePornDB-only scenes for Dillion-like lead
fid = c.execute("INSERT INTO films(title,year,cast_,kind,release_date,tpdb_id,studio) VALUES('Owned Together',2016,'Lead Star, Library Co','scene','2016-03-17','s-own','Studio A')").lastrowid
c.execute("INSERT INTO people(name,name_norm,birthday,tpdb_scenes_json,tpdb_scenes_at,tpdb_scenes_total) VALUES('Lead Star','lead star','1990-01-01',?,1,3)",
          (json.dumps([
              dict(id="s-own", title="Owned Together", date="2016-03-17", studio="Studio A", slug="owned",
                   performers=[dict(name="Lead Star", aliases=[], birthday="1990-01-01"),
                               dict(name="Library Co", aliases=[], birthday="1988-02-02")]),
              dict(id="s-out", title="Not In Library", date="2018-05-05", studio="Studio B", slug="not-in",
                   performers=[dict(name="Lead Star", aliases=[], birthday="1990-01-01"),
                               dict(name="Outside Co", aliases=["O. Co"], birthday="1991-06-06")]),
              dict(id="s-alias", title="Alias Scene", date="2019-01-01", studio="Studio B", slug="alias",
                   performers=[dict(name="Lead Star", aliases=[], birthday="1990-01-01"),
                               dict(name="Outside Co", aliases=["O. Co"], birthday="1991-06-06")]),
          ]),))
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Library Co','library co','1988-02-02')")
c.commit()
cl = app.app.test_client()
pid = {r["name"]: r["id"] for r in c.execute("SELECT id, name FROM people")}
# suggest: empty query still lists the library co-star; typed query finds the ThePornDB-only name and alias
s0 = cl.get(f"/api/people/cocredit/suggest?pid={pid['Lead Star']}").json
assert any(x["name"] == "Library Co" for x in s0["people"]), s0
assert not any(x["name"] == "Outside Co" for x in s0["people"])  # no browse-all list when the box is empty
s = cl.get(f"/api/people/cocredit/suggest?pid={pid['Lead Star']}&q=outside").json["people"]
assert s and s[0]["name"] == "Outside Co" and s[0]["n"] == 0 and s[0]["total"] == 2, s
s = cl.get(f"/api/people/cocredit/suggest?pid={pid['Lead Star']}&q=o.+co").json["people"]
assert s and s[0]["name"] == "Outside Co", s
# create the partner row as the UI would, then compare
oid = cl.get("/api/person/lookup?name=Outside Co").json["id"]
d = cl.get(f"/api/people/cocredit?ids={pid['Lead Star']},{oid}").json
titles = [r["title"] for r in d["rows"]]
assert titles == ["Not In Library", "Alias Scene"], titles
assert d["in_library"] == 0 and d["not_in_library"] == 2 and d["total"] == 2
assert all(not r["in_library"] for r in d["rows"])
# library pair still works
d2 = cl.get(f"/api/people/cocredit?ids={pid['Lead Star']},{pid['Library Co']}").json
assert {r["title"] for r in d2["rows"]} == {"Owned Together"} and d2["in_library"] == 1
# Shared scene id is enough even when the other person is missing from a cached performer list
c.execute("INSERT INTO people(name,name_norm,birthday,tpdb_scenes_json,tpdb_scenes_at) VALUES('Id Only','id only','1990-02-02',?,1)",
          (json.dumps([dict(id="s-idonly", title="Id Only Scene", date="2021-01-01", studio="S", slug="id-only",
                           performers=[dict(name="Id Only", aliases=[], birthday="1990-02-02")])]),))
# Lead's cache already has no such scene; add it WITHOUT listing Id Only on the cast
lead = json.loads(c.execute("SELECT tpdb_scenes_json FROM people WHERE name='Lead Star'").fetchone()[0])
lead.append(dict(id="s-idonly", title="Id Only Scene", date="2021-01-01", studio="S", slug="id-only",
                 performers=[dict(name="Lead Star", aliases=[], birthday="1990-01-01")]))
c.execute("UPDATE people SET tpdb_scenes_json=? WHERE name='Lead Star'", (json.dumps(lead),)); c.commit()
iid = cl.get("/api/person/lookup?name=Id Only").json["id"]
d3 = cl.get(f"/api/people/cocredit?ids={pid['Lead Star']},{iid}").json
assert "Id Only Scene" in [r["title"] for r in d3["rows"]], [r["title"] for r in d3["rows"]]
print("ok")
