"""Bulk Delete files & remove from library: multi-select flow uses the same per-item plan/delete as the
single-item action (whole folder when safe, else own video+gallery). Shared folders fall back to files
mode so a non-selected neighbour is never wiped. Items are processed one by one; a failure on one does
not stop the others. Temp dirs only — never touches a real library.
Run: python tests/test_bulk_delete_files.py"""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
cl = app.app.test_client()
root = tempfile.mkdtemp()

def mk(p, n=10):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x" * n); return p

def add(title, paths):
    c = db.connect()
    fid = c.execute("INSERT INTO films(title, kind, added_at) VALUES(?,?,?)", (title, "movie", time.time())).lastrowid
    for p in paths:
        c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)",
                  (fid, p, os.path.getsize(p) if os.path.exists(p) else 0))
    c.commit(); return fid

c = db.connect(); db.set_setting(c, "roots", [root]); c.commit()

# ---- Alone folders: both get folder mode; bulk process wipes each folder ----
a_dir = os.path.join(root, "AloneA (1999)"); av = mk(os.path.join(a_dir, "AloneA (1999).mkv"), 40)
mk(os.path.join(a_dir, "Poster.jpg"), 3)
b_dir = os.path.join(root, "AloneB (2000)"); bv = mk(os.path.join(b_dir, "AloneB (2000).mkv"), 50)
mk(os.path.join(b_dir, "Poster.jpg"), 4)
fa, fb = add("AloneA", [av]), add("AloneB", [bv])
pa = cl.get(f"/api/films/{fa}/deletefiles").json
pb = cl.get(f"/api/films/{fb}/deletefiles").json
assert pa["mode"] == "folder" and pb["mode"] == "folder", (pa, pb)
# Simulate bulk: one-by-one POST with all ok keys from the plan
results = []
for fid, plan in ((fa, pa), (fb, pb)):
    keys = [x["key"] for x in plan["items"] if x["ok"]]
    r = cl.post(f"/api/films/{fid}/deletefiles", json={"keys": keys})
    results.append(r)
assert all(r.status_code == 200 and r.json.get("removed") for r in results), [r.json for r in results]
assert not os.path.exists(a_dir) and not os.path.exists(b_dir)
c = db.connect()
assert c.execute("SELECT COUNT(*) FROM films WHERE id IN (?,?)", (fa, fb)).fetchone()[0] == 0

# ---- Shared folder with a NON-selected neighbour: files mode; neighbour + folder kept ----
shared = os.path.join(root, "Shared")
sv1 = mk(os.path.join(shared, "Sel (2001).mkv"), 30)
sv2 = mk(os.path.join(shared, "Keep (2002).mkv"), 20)  # not selected
mk(os.path.join(shared, "Poster.jpg"), 2)
fs, fk = add("Sel", [sv1]), add("Keep", [sv2])
ps = cl.get(f"/api/films/{fs}/deletefiles").json
assert ps["mode"] == "files" and "Another library item" in (ps["folder_reason"] or ""), ps
keys = [x["key"] for x in ps["items"] if x["ok"]]
r = cl.post(f"/api/films/{fs}/deletefiles", json={"keys": keys})
assert r.status_code == 200 and r.json["mode"] == "files" and r.json["removed"], r.json
assert not os.path.exists(sv1) and os.path.exists(sv2) and os.path.isdir(shared)
assert os.path.exists(os.path.join(shared, "Poster.jpg"))  # folder not wiped
c = db.connect()
assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fs,)).fetchone()[0] == 0
assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fk,)).fetchone()[0] == 1

# ---- Two selected items sharing a folder: both files-mode; both videos gone; folder kept ----
shared2 = os.path.join(root, "Shared2")
x1 = mk(os.path.join(shared2, "X1 (2003).mkv"), 11)
x2 = mk(os.path.join(shared2, "X2 (2004).mkv"), 12)
mk(os.path.join(shared2, "extra.txt"), 5)
fx1, fx2 = add("X1", [x1]), add("X2", [x2])
p1 = cl.get(f"/api/films/{fx1}/deletefiles").json
p2 = cl.get(f"/api/films/{fx2}/deletefiles").json
assert p1["mode"] == "files" and p2["mode"] == "files"
# Bulk one-by-one (failure on one must not stop the other — exercise with a spy that fails the first video)
real_delete = app.delete_paths
calls = []
def spy_fail_first(paths):
    calls.append(list(paths))
    if len(calls) == 1:
        return {p: "Access denied" for p in paths}
    return real_delete(paths)
app.delete_paths = spy_fail_first
r1 = cl.post(f"/api/films/{fx1}/deletefiles", json={"keys": [x["key"] for x in p1["items"] if x["ok"]]})
r2 = cl.post(f"/api/films/{fx2}/deletefiles", json={"keys": [x["key"] for x in p2["items"] if x["ok"]]})
app.delete_paths = real_delete
assert r1.status_code == 500 and r1.json.get("removed") is False, r1.json  # first failed, kept
assert r2.status_code == 200 and r2.json.get("removed") is True, r2.json   # second still ran
assert os.path.exists(x1) and not os.path.exists(x2) and os.path.isdir(shared2)
assert os.path.exists(os.path.join(shared2, "extra.txt"))
c = db.connect()
assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fx1,)).fetchone()[0] == 1
assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fx2,)).fetchone()[0] == 0

# ---- After neighbour gone, Keep may become folder mode; leftover fx1 alone in Shared2 too ----
pk = cl.get(f"/api/films/{fk}/deletefiles").json
px = cl.get(f"/api/films/{fx1}/deletefiles").json
assert pk["mode"] in ("folder", "files") and px["mode"] in ("folder", "files"), (pk, px)

# UI markers present
html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "index.html"), encoding="utf-8").read()
assert "openBulkDeleteFiles" in html and "function bdfGo" in html
assert "fellBack" in html and "Folder kept:" in html
assert 'onclick="openBulkDeleteFiles()"' in html

print("OK test_bulk_delete_files")
