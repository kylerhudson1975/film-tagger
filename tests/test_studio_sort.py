"""Studio sort: within a studio, first billed performer A-Z (folded, none last), then newest release, then id.
Run: python -m pytest tests/test_studio_sort.py"""
import os, sys, tempfile
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db

def add(c, title, studio, cast, date):
    return c.execute("INSERT INTO films(title,kind,studio,cast_,release_date) VALUES(?, 'scene', ?, ?, ?)", (title, studio, cast, date)).lastrowid

def test_studio_sort_ties():
    c = db.connect()
    add(c, "z1", "Beta", "Zoe Last", "2020-01-01")
    add(c, "none", "Alpha", None, "2024-01-01")
    add(c, "bob-old", "Alpha", "bob Early, Zed", "2019-01-01")
    add(c, "eli", "Alpha", "Élise Accent", "2018-01-01")
    add(c, "bob-new", "Alpha", "Bob Early", "2023-05-05")
    add(c, "anna", "Alpha", "anna a", None)
    add(c, "empty", "Alpha", "", "2025-01-01")
    c.commit(); cl = app.app.test_client()
    t = lambda q: [f["title"] for f in cl.get("/api/films?sort=studio&limit=100" + q).json["films"]]
    asc = t("")
    assert asc == ["anna", "bob-new", "bob-old", "eli", "none", "empty", "z1"] or asc == ["anna", "bob-new", "bob-old", "eli", "empty", "none", "z1"]
    assert asc.index("eli") > asc.index("bob-old") and asc[-1] == "z1"
    assert t("&order=desc")[:1] == ["z1"] and t("&order=desc")[1:4] == ["anna", "bob-new", "bob-old"]
    # pagination is stable
    page = [f["title"] for f in cl.get("/api/films?sort=studio&limit=3&offset=0").json["films"]] + \
           [f["title"] for f in cl.get("/api/films?sort=studio&limit=4&offset=3").json["films"]]
    assert page == asc
