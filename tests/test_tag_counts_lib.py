"""Tag counts follow the library folder (?lib=): /api/tags?lib= counts approved titles in that folder only (same folder
match as /api/films?lib=; all scene roots are one library), n_all keeps the whole-library count; no lib = unchanged.
Run: python tests/test_tag_counts_lib.py"""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
cl = app.app.test_client()
c = db.connect()
db.set_setting(c, "roots", [r"D:\Movies", r"E:\TV"]); db.set_setting(c, "scene_roots", [r"D:\Porn Scenes", r"F:\Scenes2"])
tid = {}
def add(title, path, tags, kind="film", status="approved"):
    fid = c.execute("INSERT INTO films(title,kind) VALUES(?,?)", (title, kind)).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path))
    for t in tags:
        if t not in tid: tid[t] = c.execute("INSERT INTO tags(name,category) VALUES(?,?)", (t, "Mood")).lastrowid
        c.execute("INSERT INTO film_tags(film_id,tag_id,source,status) VALUES(?,?,?,?)", (fid, tid[t], "manual", status))
add("A", r"D:\Movies\A.mkv", ["Dark", "Funny"])
add("B", r"D:\Movies\Sub\B.mkv", ["Dark"])
add("C", r"E:\TV\C.mkv", ["Dark", "Funny"])
add("D", r"D:\Movies_old\D.mkv", ["Dark"])          # sibling folder with the same prefix: not in D:\Movies
add("E", r"D:\Porn Scenes\Studio\E.mp4", ["Dark"], "scene")
add("F", r"F:\Scenes2\Studio\F.mp4", ["Dark"], "scene")
add("G", r"D:\Movies\G.mkv", ["Funny"], status="pending")  # pending tags never count
add("H", r"D:\Movies\H.mkv", ["Fun-ny"])          # spelling variant: the film list's tag filter treats it as 'Funny'
c.commit()
def counts(lib=None):
    return {t["name"]: (t["n"], t.get("n_all")) for t in cl.get("/api/tags", query_string={"lib": lib} if lib else {}).json}
assert counts() == {"Dark": (6, None), "Funny": (2, None), "Fun-ny": (1, None)}, counts()  # unscoped unchanged (per tag id)
assert counts(r"D:\Movies") == {"Dark": (2, 6), "Funny": (2, 2), "Fun-ny": (2, 1)}, counts(r"D:\Movies")
assert counts("d:\\movies\\") == counts(r"D:\Movies")
assert counts(r"D:\Porn Scenes") == {"Dark": (2, 6), "Funny": (0, 2), "Fun-ny": (0, 1)}
assert counts(r"F:\Scenes2\Studio") == {"Dark": (2, 6), "Funny": (0, 2), "Fun-ny": (0, 1)}
assert counts(r"X:\Nowhere") == {"Dark": (0, 6), "Funny": (0, 2), "Fun-ny": (0, 1)}
for lib in [r"D:\Movies", r"E:\TV", r"D:\Porn Scenes", r"D:\Movies\Sub"]:  # count == titles the film list returns for that tag in that folder
    for t, (n, _) in counts(lib).items():
        assert cl.get("/api/films", query_string={"lib": lib, "all": t}).json["total"] == n, (lib, t)
print("ok")
