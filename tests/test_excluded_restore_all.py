"""Restoring a removed file also forgets earlier removals of the same file (same size + fingerprint, or same name +
size), so it really comes back; other removed files of the same size stay removed. Temp files only.
Run: python tests/test_excluded_restore_all.py"""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
app.kick_focus = lambda: None
cl = app.app.test_client()
root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "roots", [root]); c.commit()
new = os.path.join(root, "Studio B", "Studio B - 2025-07-01 - A, B - Right Title.mp4"); os.makedirs(os.path.dirname(new))
open(new, "wb").write(os.urandom(300 * 1024)); size = os.path.getsize(new); h = db.file_hash(new)
other = os.path.join(root, "Studio B", "Different.mp4"); open(other, "wb").write(os.urandom(size))
now = time.time()
for path, hh in ((os.path.join(root, "Studio B", "Old name.mp4"), h), (os.path.join(root, "Studio A", "Wrong name.mp4"), h),
                 (other, db.file_hash(other))):
    c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at) VALUES(?,?,?,?,?,?)", (path, size, hh, "x", "scene", now))
c.commit()
assert db.is_excluded(c, new, size) and db.is_excluded(c, other, size)
cl.post("/api/scan", json={"roots": [root]})
assert db.connect().execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0  # removed (by fingerprint) stays removed
xid = db.connect().execute("SELECT id FROM excluded WHERE path LIKE '%Wrong name%'").fetchone()[0]
r = cl.post(f"/api/excluded/{xid}/restore"); assert r.status_code == 200 and r.json["added"] is True and r.json["also_forgotten"] == 1, r.json
c = db.connect()
assert c.execute("SELECT COUNT(*) FROM files WHERE path=?", (new,)).fetchone()[0] == 1
assert c.execute("SELECT COUNT(*) FROM files WHERE path=?", (other,)).fetchone()[0] == 0 and db.is_excluded(c, other, size)  # unrelated file stays removed
assert c.execute("SELECT COUNT(*) FROM excluded").fetchone()[0] == 1
print("OK test_excluded_restore_all")
