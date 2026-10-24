"""Delete files & remove from library: lists only the item's own video file(s) and gallery inside library roots,
refuses anything else, permanently deletes only what it listed, removes the item like Remove (stays on the removed
list), and on a failed deletion keeps the item only while its video still exists. Temp folders only.
Run: python tests/test_delete_files.py"""
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
real_delete = app.delete_paths; calls = []
def spy(paths): calls.append(list(paths)); return real_delete(paths)
app.delete_paths = spy
assert cl.post(f"/api/films/{fo}/deletefiles", json={"keys": ["v0"]}).status_code == 400 and not calls and os.path.exists(os.path.join(outside, "O.mkv"))
assert cl.post(f"/api/films/{fa}/deletefiles", json={"keys": ["v9"]}).status_code == 400 and not calls
fm = add("Missing", [os.path.join(root, "gone.mkv")]); x = cl.get(f"/api/films/{fm}/deletefiles").json["items"][0]; assert not x["ok"]
g2 = os.path.join(root, "G", "Gam (2003).mkv"); mk(g2); gg = os.path.join(root, "G", "Gam (2003) - GALLERY"); mk(os.path.join(gg, "clip.mp4"))
fg = add("Gam", [g2]); x = [y for y in cl.get(f"/api/films/{fg}/deletefiles").json["items"] if y["kind"] == "gallery"][0]
assert not x["ok"] and "video" in x["reason"]  # a gallery holding videos is never deleted
db_ = lambda: db.connect()
# failure while the video still exists -> item kept, nothing on the removed list
app.delete_paths = lambda paths: {p: ("Access denied" if p == a else real_delete([p])[p]) for p in paths}
r = cl.post(f"/api/films/{fa}/deletefiles", json={"keys": ["v0", "g0"]})
assert r.status_code == 500 and r.json["failed"] == [{"path": a, "error": "Access denied"}] and r.json["deleted"] == [ga] and r.json["removed"] is False
assert os.path.exists(a) and not os.path.exists(ga) and os.path.exists(b)
c = db_(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fa,)).fetchone()[0] == 1 and c.execute("SELECT COUNT(*) FROM excluded").fetchone()[0] == 0
# failure on the gallery after the video was deleted -> item removed (never points at a missing file)
mk(os.path.join(ga, "1.jpg"))
app.delete_paths = lambda paths: {p: ("In use" if p == ga else real_delete([p])[p]) for p in paths}
r = cl.post(f"/api/films/{fa}/deletefiles", json={"keys": ["v0", "g0"]})
assert r.status_code == 500 and r.json["removed"] is True and r.json["deleted"] == [a] and not os.path.exists(a) and os.path.isdir(ga)
c = db_(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fa,)).fetchone()[0] == 0 and c.execute("SELECT COUNT(*) FROM excluded WHERE path=?", (a,)).fetchone()[0] == 1
# success: real permanent delete of exactly the ticked, listed paths; neighbour in the same folder untouched
mk(a, 100); fa2 = add("Alpha again", [a]); app.delete_paths = spy; calls.clear()
r = cl.post(f"/api/films/{fa2}/deletefiles", json={"keys": ["v0", "g0"]}); assert r.status_code == 200 and r.json["removed"], r.json
assert calls == [[a, ga]] and not os.path.exists(a) and not os.path.exists(ga) and os.path.exists(b)
c = db_(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fa2,)).fetchone()[0] == 0 and c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fb,)).fetchone()[0] == 1
# gallery unticked: video only
mk(a, 100); mk(os.path.join(ga, "1.jpg")); fa3 = add("Alpha third", [a]); calls.clear()
assert cl.post(f"/api/films/{fa3}/deletefiles", json={"keys": ["v0"]}).status_code == 200 and calls == [[a]] and os.path.isdir(ga) and not os.path.exists(a)
# delete_paths itself: file, folder, missing
t = tempfile.mkdtemp(); f1 = mk(os.path.join(t, "f.bin")); d1 = os.path.join(t, "dir"); mk(os.path.join(d1, "x", "y.txt"))
r = real_delete([f1, d1, os.path.join(t, "nope")]); assert r[f1] is None and r[d1] is None and r[os.path.join(t, "nope")] == "Not found"
assert not os.path.exists(f1) and not os.path.exists(d1)
print("OK test_delete_files")
