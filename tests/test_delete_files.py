"""Delete files & remove from library: prefers wiping the film's containing folder (permanent delete, not
Recycle Bin) when safe; otherwise lists only the item's own video file(s) and gallery inside library roots,
refuses anything else, permanently deletes only what it listed, removes the item like Remove (stays on the
removed list), and on a failed deletion keeps the item only while its video still exists. Temp folders only.
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
# Shared folder with another film -> files mode (folder kept); own video + own gallery with sizes
j = cl.get(f"/api/films/{fa}/deletefiles").json
assert j["mode"] == "files" and "Another library item" in (j["folder_reason"] or ""), j
it = {x["kind"]: x for x in j["items"]}
assert it["video"]["path"] == a and it["video"]["ok"] and it["video"]["size"] == 100
assert it["gallery"]["path"] == ga and it["gallery"]["ok"] and it["gallery"]["count"] == 2 and it["gallery"]["size"] == 15
assert all(b not in x["path"] for x in j["items"])
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
# Film alone under root/G: folder mode (gallery with a video inside is wiped with the whole folder)
g2 = os.path.join(root, "G", "Gam (2003).mkv"); mk(g2); gg = os.path.join(root, "G", "Gam (2003) - GALLERY"); mk(os.path.join(gg, "clip.mp4"))
fg = add("Gam", [g2]); jg = cl.get(f"/api/films/{fg}/deletefiles").json
assert jg["mode"] == "folder" and jg["items"][0]["kind"] == "folder" and jg["items"][0]["path"] == os.path.abspath(os.path.join(root, "G")), jg
# Old files-mode gallery guard still applies when folder mode is refused (shared folder + gallery with video)
g_shared = os.path.join(shared, "GamShared (2003).mkv"); mk(g_shared)
ggs = os.path.join(shared, "GamShared (2003) - GALLERY"); mk(os.path.join(ggs, "clip.mp4"))
fgs = add("GamShared", [g_shared])
x = [y for y in cl.get(f"/api/films/{fgs}/deletefiles").json["items"] if y["kind"] == "gallery"][0]
assert not x["ok"] and "video" in x["reason"]  # a gallery holding videos is never deleted in files mode
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
r = cl.post(f"/api/films/{fa2}/deletefiles", json={"keys": ["v0", "g0"]}); assert r.status_code == 200 and r.json["removed"] and r.json["mode"] == "files", r.json
assert calls == [[a, ga]] and not os.path.exists(a) and not os.path.exists(ga) and os.path.exists(b)
c = db_(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fa2,)).fetchone()[0] == 0 and c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fb,)).fetchone()[0] == 1
# gallery unticked: video only
mk(a, 100); mk(os.path.join(ga, "1.jpg")); fa3 = add("Alpha third", [a]); calls.clear()
assert cl.post(f"/api/films/{fa3}/deletefiles", json={"keys": ["v0"]}).status_code == 200 and calls == [[a]] and os.path.isdir(ga) and not os.path.exists(a)
# delete_paths itself: file, folder, missing
t = tempfile.mkdtemp(); f1 = mk(os.path.join(t, "f.bin")); d1 = os.path.join(t, "dir"); mk(os.path.join(d1, "x", "y.txt"))
r = real_delete([f1, d1, os.path.join(t, "nope")]); assert r[f1] is None and r[d1] is None and r[os.path.join(t, "nope")] == "Not found"
assert not os.path.exists(f1) and not os.path.exists(d1)

# ---- whole-folder delete when the film is alone in its folder ----
alone_dir = os.path.join(root, "Alone (1999)")
av = mk(os.path.join(alone_dir, "Alone (1999).mkv"), 80)
mk(os.path.join(alone_dir, "Poster.jpg"), 5)
mk(os.path.join(alone_dir, "Alone (1999).nfo"), 3)
ag = os.path.join(alone_dir, "Alone (1999) - GALLERY"); mk(os.path.join(ag, "1.jpg"), 4)
fal = add("Alone", [av])
jal = cl.get(f"/api/films/{fal}/deletefiles").json
assert jal["mode"] == "folder" and jal["folder_reason"] is None, jal
assert len(jal["items"]) == 1 and jal["items"][0]["kind"] == "folder" and jal["items"][0]["ok"]
assert jal["items"][0]["path"] == os.path.abspath(alone_dir)
assert jal["items"][0]["count"] >= 4
app.delete_paths = spy; calls.clear()
r = cl.post(f"/api/films/{fal}/deletefiles", json={"keys": ["f0"]})
assert r.status_code == 200 and r.json["removed"] and r.json["mode"] == "folder", r.json
assert calls == [[os.path.abspath(alone_dir)]] and not os.path.exists(alone_dir) and os.path.exists(b)
c = db_(); assert c.execute("SELECT COUNT(*) FROM films WHERE id=?", (fal,)).fetchone()[0] == 0

# ---- guard: another film shares the folder -> files mode, folder kept ----
# (Alpha/Beta Shared case already covers this; assert the reason explicitly)
assert "Another library item" in (cl.get(f"/api/films/{fb}/deletefiles").json["folder_reason"] or "")

# ---- guard: film sitting directly in a library root -> files mode (root refused) ----
rv = mk(os.path.join(root, "RootFilm (2010).mkv"), 40)
fr = add("RootFilm", [rv])
jr = cl.get(f"/api/films/{fr}/deletefiles").json
assert jr["mode"] == "files" and "library folder" in (jr["folder_reason"] or "").lower(), jr
assert jr["items"][0]["kind"] == "video" and jr["items"][0]["ok"]
app.delete_paths = spy; calls.clear()
assert cl.post(f"/api/films/{fr}/deletefiles", json={"keys": ["v0"]}).status_code == 200
assert calls == [[rv]] and not os.path.exists(rv) and os.path.isdir(root)

# ---- df_folder_guard helpers: drive root / protected ----
assert app.df_folder_guard(root, [root], set()) == "That is a library folder"
assert "drive root" in (app.df_folder_guard(os.path.abspath(os.sep), [root], set()) or "").lower() or \
       "library folder" in (app.df_folder_guard(os.path.abspath(os.sep), [root], set()) or "").lower() or \
       "Not inside" in (app.df_folder_guard(os.path.abspath(os.sep), [root], set()) or "")

print("OK test_delete_files")
