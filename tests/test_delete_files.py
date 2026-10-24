"""Delete files & remove from library: lists only the item's own video file(s) and gallery inside library roots,
refuses anything else, recycles (mocked here) only what it listed, removes the item like Remove (stays on the removed
list) after success, and keeps the item when any file fails. Temp folders only. Run: python tests/test_delete_files.py"""
import os, sys, tempfile, shutil
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
cl = app.app.test_client()
root = tempfile.mkdtemp(); outside = tempfile.mkdtemp()
def mk(p, n=10):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(b"x" * n); return p
shared = os.path.join(root, "Shared")
a = mk(os.path.join(shared, "Alpha (2001).mkv"), 100); b = mk(os.path.join(shared, "Beta (2002).mkv"), 50)
ga = os.path.join(shared, "Alpha (2001) - GALLERY"); mk(os.path.join(ga, "1.jpg"), 7); mk(os.path.join(ga, "2.jpg"), 8)
c = db.connect(); db.set_setting(c, "roots", [root]); c.commit()
def add(title, paths):
    c = db.connect(); cur = c.execute("INSERT INTO films(title, kind) VALUES(?, 'movie')", (title,)); fid = cur.lastrowid
    for p in paths: c.execute("INSERT INTO files(film_id, path, size) VALUES(?,?,?)", (fid, p, os.path.getsize(p) if os.path.exists(p) else 0))
    c.commit(); return fid
fa = add("Alpha", [a]); fb = add("Beta", [b])
# path safety helper
assert app.df_inside_roots(a, [root]) and not app.df_inside_roots(root, [root]) and not app.df_inside_roots(os.path.join(outside, "x.mkv"), [root])
assert not app.df_inside_roots(os.path.join(root, "..", os.path.basename(outside), "x.mkv"), [root])
# list endpoint: own video + own gallery with sizes; the neighbour in the same folder is not listed
it = {x["kind"]: x for x in cl.get(f"/api/films/{fa}/deletefiles").json["items"]}
assert it["video"]["path"] == a and it["video"]["ok"] and it["video"]["size"] == 100
assert it["gallery"]["path"] == ga and it["gallery"]["ok"] and it["gallery"]["count"] == 2 and it["gallery"]["size"] == 15
assert all(b not in x["path"] for x in cl.get(f"/api/films/{fa}/deletefiles").json["items"])
assert cl.get("/api/films/99999/deletefiles").status_code == 404
# outside the roots / missing / shared with another item: listed but refused
fo = add("Outside", [mk(os.path.join(outside, "O.mkv"))])
x = cl.get(f"/api/films/{fo}/deletefiles").json["items"][0]; assert not x["ok"] and "library folder" in x["reason"]
calls = []
def fake_ok(paths): calls.append(list(paths)); [shutil.rmtree(p) if os.path.isdir(p) else os.remove(p) for p in paths]; return {p: None for p in paths}
app.recycle = fake_ok
assert cl.post(f"/api/films/{fo}/deletefiles", json={"keys": ["v0"]}).status_code == 400 and not calls and os.path.exists(os.path.join(outside, "O.mkv"))
assert cl.post(f"/api/films/{fa}/deletefiles", json={"keys": ["v9"]}).status_code == 400 and not calls
fm = add("Missing", [os.path.join(root, "gone.mkv")]); x = cl.get(f"/api/films/{fm}/deletefiles").json["items"][0]; assert not x["ok"]
g2 = os.path.join(root, "G", "Gam (2003).mkv"); mk(g2); gg = os.path.join(root, "G", "Gam (2003) - GALLERY"); mk(os.path.join(gg, "clip.mp4"))
fg = add("Gam", [g2]); x = [y for y in cl.get(f"/api/films/{fg}/deletefiles").json["items"] if y["kind"] == "gallery"][0]
assert not x["ok"] and "video" in x["reason"]  # a gallery holding videos is never deleted
# failure keeps the item (and its files table rows), nothing goes on the removed list
app.recycle = lambda paths: {p: ("Access denied" if p == ga else None) for p in paths}
r = cl.post(f"/api/films/{fa}/deletefiles", json={"keys": ["v0", "g0"]})
assert r.status_code == 500 and r.json["failed"] == [{"path": ga, "error": "Access denied"}] and r.json["recycled"] == [a]
c = db.connect(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fa,)).fetchone()[0] == 1
assert c.execute("SELECT COUNT(*) FROM files WHERE film_id=?", (fa,)).fetchone()[0] == 1 and c.execute("SELECT COUNT(*) FROM excluded").fetchone()[0] == 0
# success: only the ticked, listed paths are recycled; item removed and remembered; neighbour untouched
app.recycle = fake_ok
r = cl.post(f"/api/films/{fa}/deletefiles", json={"keys": ["v0", "g0"]}); assert r.status_code == 200, r.json
assert calls == [[a, ga]] and not os.path.exists(a) and not os.path.exists(ga) and os.path.exists(b)
c = db.connect(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fa,)).fetchone()[0] == 0
assert c.execute("SELECT COUNT(*) FROM excluded WHERE path=?", (a,)).fetchone()[0] == 1
assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fb,)).fetchone()[0] == 1
# gallery unticked: video only
mk(a, 100); mk(os.path.join(ga, "1.jpg")); fa2 = add("Alpha again", [a]); calls.clear()
assert cl.post(f"/api/films/{fa2}/deletefiles", json={"keys": ["v0"]}).status_code == 200 and calls == [[a]] and os.path.isdir(ga)
# the real recycler (restored) refuses instead of deleting when there is no Recycle Bin (any non-Windows OS)
import importlib; importlib.reload(app)
if os.name != "nt":
    p = mk(os.path.join(root, "keep.mkv")); assert app.recycle([p])[p] and os.path.exists(p)
print("OK test_delete_files")
