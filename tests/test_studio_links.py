"""Related studios: a ThePornDB sub-label ('Out of the Family') counts as Kyle's studio ("Devil's Film") in matching,
and a match never replaces his studio. HTTP mocked. Run: python -m pytest tests/test_studio_links.py"""
import os, sys, tempfile, json
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb

SC = {"id": "6ddb", "title": "That Time I Got My Stepmom Pregnant - Annie King & Elias Cash", "date": "2024-09-26",
      "site": {"name": "Out of the Family", "parent": {"name": "Adult Time"}, "network": {"name": "Adult Time"}},
      "performers": [{"name": "Annie King"}, {"name": "Elias Cash"}], "tags": []}

def test_link_makes_studio_match():
    c = db.connect(); tpdb._STUDIO_CACHE.clear()
    t = "That Time I Got My Stepmom Pregnant Annie King"
    s0, why0 = tpdb.score(SC, "Devil's Film", None, "Devil's Film " + t)
    assert "studio" not in why0
    cl = app.app.test_client()
    assert cl.post("/api/studio-links", json={"child": "Out of the Family", "parent": "Devil's Film"}).json["ok"]
    assert cl.get("/api/studio-links").json["links"] == [{"child": "Out of the Family", "parent": "Devil's Film"}]
    s1, why1 = tpdb.score(SC, "Devil's Film", None, "Devil's Film " + t)
    assert "studio" in why1 and s1 == s0 + 3
    assert db.related_studios("Out of the Family") == ["Devil's Film"]

def test_tpdb_parent_counts_without_link():
    s, why = tpdb.score(SC, "Adult Time", None, "Adult Time That Time I Got My Stepmom Pregnant Annie King")
    assert "studio" in why

def test_apply_keeps_kyles_studio(monkeypatch):
    c = db.connect()
    db.add_studio_link(c, "Out of the Family", "Devil's Film")
    fid = c.execute("INSERT INTO films(title,kind,studio) VALUES(?, 'scene', ?)", ("x", "Devil's Film")).lastrowid
    c.commit()
    app.apply_scene(c, fid, SC); c.commit()
    r = c.execute("SELECT studio, title, release_date, updated_at FROM films WHERE id=?", (fid,)).fetchone()
    assert r[0] == "Devil's Film" and r[1].startswith("That Time") and r[2] == "2024-09-26" and isinstance(r[3], float)

def test_apply_sets_studio_when_unrelated_or_empty():
    c = db.connect()
    fid = c.execute("INSERT INTO films(title,kind,studio) VALUES('y','scene',NULL)").lastrowid; c.commit()
    app.apply_scene(c, fid, SC); c.commit()
    assert c.execute("SELECT studio FROM films WHERE id=?", (fid,)).fetchone()[0] == "Out of the Family"
