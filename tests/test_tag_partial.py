"""Sidebar tag boxes (ALL / ANY / NOT) accept part of a tag name: 'object' finds titles tagged 'Object insertion'
(case/accent-insensitive). A term that names a tag still matches only that tag ('Cum' never matches 'Cumshot').
The partial match also narrows a person's 'Credits in my library' list (?pin=). Run: python tests/test_tag_partial.py"""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
cl = app.app.test_client(); c = db.connect()
db.set_setting(c, "scene_roots", [r"D:\Porn Scenes"])
def add(title, cast, tags, kind="scene"):
    fid = c.execute("INSERT INTO films(title,kind,cast_,year,release_date) VALUES(?,?,?,?,?)", (title, kind, cast, 2020, "2020-05-01")).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, rf"D:\Porn Scenes\Studio\{title}.mp4"))
    for t in tags: db.add_tag(c, fid, t, "Content")
    return fid
add("Scene A", "Penny Test", ["Object insertion", "Solo"])
add("Scene B", "Penny Test", ["Cumshot"])
add("Scene C", "Other Person", ["Objects (props)"])
add("Scene D", "Other Person", ["Cum", "Ébène"])
c.commit()
pid = cl.get("/api/person/lookup?name=Penny Test").json["id"]
c.execute("UPDATE people SET birthday='1995-01-01' WHERE id=?", (pid,)); c.commit()
got = lambda **q: sorted(f["title"] for f in cl.get("/api/films", query_string=dict(limit=100, **q)).json["films"])
assert got(all="object") == ["Scene A", "Scene C"], got(all="object")
assert got(all="OBJECT") == ["Scene A", "Scene C"]
assert got(all="insert") == ["Scene A"]                     # middle of a name
assert got(all="Object insertion") == ["Scene A"]           # whole name still exact
assert got(any="insertion, ebene") == ["Scene A", "Scene D"]  # OR, accent-insensitive
assert got(none="object") == ["Scene B", "Scene D"]
assert got(all="Cum") == ["Scene D"]                        # an existing tag name is not widened to 'Cumshot'
assert got(all="nosuchtag") == []
assert got(pin=str(pid)) == ["Scene A", "Scene B"], got(pin=str(pid))
assert got(pin=str(pid), all="object") == ["Scene A"]       # Penny's credits narrowed by the partial tag
assert got(pin=str(pid), q="object") == ["Scene A"]         # the main search box agrees
print("ok")
