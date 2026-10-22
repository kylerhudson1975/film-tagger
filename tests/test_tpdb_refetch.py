"""Fetch on an already-matched scene shows choices: the current match (by its tpdb_id) first, then candidates from a
query built from the DB fields. Nothing is applied until a pick. HTTP is mocked. Run: python -m pytest tests/test_tpdb_refetch.py"""
import os, sys, tempfile, json
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import app, db, tpdb

def sc(i, title): return {"id": i, "title": title, "date": "2020-02-02", "site": {"name": "Some Studio"}, "performers": [{"name": "Ann Example"}]}

def mk(c, tid):
    fid = c.execute("INSERT INTO films(title,kind,tpdb_id,studio,release_date,cast_,updated_at) VALUES('Old Title','scene',?,'Some Studio','2020-02-02','Ann Example, Bea Sample',1)", (tid,)).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, os.path.join(tempfile.mkdtemp(), "Some Studio - 2020-02-02 - Ann Example - Old Title.mp4")))
    db.set_setting(c, "tpdb_key", "dummy"); c.commit(); return fid

def test_matched_scene_lists_current_first_and_applies_nothing(monkeypatch):
    app.TPC.clear(); seen = {}
    monkeypatch.setattr(tpdb, "get_scene", lambda tok, sid: sc(sid, "Current One"))
    def cands(tok, path, query=None, limit=8, **k):
        seen["query"] = query; seen["path"] = path
        return dict(query=query, results=[dict(scene=sc("other", "Other Scene"), score=5, why=[], confident=False),
                                          dict(scene=sc("cur-1", "Current One"), score=4, why=[], confident=False)])
    monkeypatch.setattr(tpdb, "candidates", cands)
    monkeypatch.setattr(tpdb, "lookup", lambda *a, **k: pytest.fail("no auto identify"))
    c = db.connect(); fid = mk(c, "cur-1")
    d = app.app.test_client().get(f"/api/films/{fid}/tpdb/candidates").json
    assert seen["query"] is None and seen["path"] == "Some Studio/2020-02-02 Ann Example Bea Sample Old Title.mp4"
    assert [x["id"] for x in d["results"]] == ["cur-1", "other"] and d["results"][0]["current"] and d["matched"]
    row = c.execute("SELECT title, tpdb_id, updated_at FROM films WHERE id=?", (fid,)).fetchone()
    assert tuple(row) == ("Old Title", "cur-1", 1)

def test_current_lookup_fails_still_lists_choices(monkeypatch):
    app.TPC.clear()
    def boom(tok, sid): raise OSError("down")
    monkeypatch.setattr(tpdb, "get_scene", boom)
    monkeypatch.setattr(tpdb, "candidates", lambda tok, path, query=None, limit=8, **k: dict(query=query, results=[dict(scene=sc("x", "X"), score=1, why=[], confident=False)]))
    c = db.connect(); fid = mk(c, "cur-2")
    d = app.app.test_client().get(f"/api/films/{fid}/tpdb/candidates").json
    assert d["results"][0]["current"] and "could not be loaded" in d["results"][0]["title"] and d["results"][1]["id"] == "x"

def test_pick_other_replaces_id(monkeypatch):
    monkeypatch.setattr(tpdb, "get_scene", lambda tok, sid: sc(sid, "Picked Scene"))
    c = db.connect(); fid = mk(c, "cur-3")
    r = app.app.test_client().post(f"/api/films/{fid}/tpdb", json={"tpdb_id": "new-9", "keep": [], "rename": False})
    assert r.status_code == 200 and r.json["tpdb_id"] == "new-9" and r.json["title"] == "Picked Scene"
    assert isinstance(c.execute("SELECT updated_at FROM films WHERE id=?", (fid,)).fetchone()[0], float)

def test_unmatched_scene_uses_file_name(monkeypatch):
    app.TPC.clear(); seen = {}
    monkeypatch.setattr(tpdb, "get_scene", lambda *a: pytest.fail("no id lookup for unmatched"))
    monkeypatch.setattr(tpdb, "candidates", lambda tok, path, query=None, limit=8, **k: seen.update(q=query) or dict(query="", results=[]))
    c = db.connect(); fid = mk(c, "")
    d = app.app.test_client().get(f"/api/films/{fid}/tpdb/candidates").json
    assert seen["q"] is None and d["matched"] is False
