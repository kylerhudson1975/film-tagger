"""Upgrade file through the app: POST /api/films/<id>/upgrade-file is a dry run unless dry_run is false; the real run
keeps the item and repoints its file; /upgrade-file/subtitles renames only the confirmed subtitle files. The Recycle Bin
is faked (nothing is deleted for real). Temp folders only. Run: python tests/test_upgrade_file_api.py"""
import os, sys, struct, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, upgrade
cl = app.app.test_client()

def box(t, payload): return struct.pack(">I", 8 + len(payload)) + t + payload
def mp4(path, w, h, seconds, pad=0):
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">II", 1000, int(seconds * 1000)) + bytes(80))
    tkhd = box(b"tkhd", bytes(76) + struct.pack(">II", w << 16, h << 16))
    data = box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", mvhd + box(b"trak", tkhd + box(b"mdia", box(b"hdlr", bytes(8) + b"vide" + bytes(12))))) + box(b"free", bytes(pad))
    os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(data); return path

root = tempfile.mkdtemp(); outside = tempfile.mkdtemp()
c = db.connect(); db.set_setting(c, "roots", [root]); c.commit()
old = mp4(os.path.join(root, "Film (1999)", "Film (1999).mp4"), 1280, 720, 5400)
sub = os.path.join(root, "Film (1999)", "Film (1999).srt"); open(sub, "w").write("1\n")
db.scan(c, [root]); fid, file_id = c.execute("SELECT film_id, id FROM files").fetchone()
c.execute("UPDATE films SET watched=1, notes='keep me' WHERE id=?", (fid,)); c.commit()
new = mp4(os.path.join(outside, "Film (1999) 1080p.mp4"), 1920, 1080, 5400, pad=4000)
binned = []
upgrade.recycle_problem = lambda p: None
upgrade.recycle = lambda p: (binned.append(p), os.remove(p))[0]
u = f"/api/films/{fid}/upgrade-file"
# dry run (also the default): compare only
for body in ({"path": new}, {"path": new, "dry_run": True, "file_id": file_id}):
    r = cl.post(u, json=body); assert r.status_code == 200, r.json
    j = r.json; assert j["dry_run"] and j["old"]["resolution"] == "720p" and j["new"]["resolution"] == "1080p" and not j["inside_library"]
    assert j["move_possible"] and j["summary"] == "Upgraded 720p → 1080p" and j["recycle"]["possible"]
assert os.path.exists(old) and os.path.exists(new) and not binned and c.execute("SELECT path FROM files").fetchone()[0] == old
assert cl.post(u, json={"path": os.path.join(outside, "missing.mkv")}).status_code == 400
assert cl.post("/api/films/99999/upgrade-file", json={"path": new}).status_code == 404
# real run, moved into the old file's folder
r = cl.post(u, json={"path": new, "file_id": file_id, "move": True, "dry_run": False}); assert r.status_code == 200, r.json
j = r.json; target = os.path.join(root, "Film (1999)", "Film (1999) 1080p.mp4")
assert j["ok"] and j["summary"] == "Upgraded 720p → 1080p" and j["path"] == target and j["recycled"] and binned == [old]
f = cl.get(f"/api/films/{fid}").json
assert f["id"] == fid and f["files"][0]["path"] == target and f["resolution"] == "1080p" and f["watched"] == 1 and f["notes"] == "keep me"
assert [s["path"] for s in j["sidecars"]] == [sub] and os.path.exists(sub)
# subtitles only on request
r = cl.post(u + "/subtitles", json={"file_id": file_id, "old_path": old, "paths": [sub]}); assert r.status_code == 200 and not r.json["errors"], r.json
assert not os.path.exists(sub) and os.path.exists(os.path.join(root, "Film (1999)", "Film (1999) 1080p.srt"))
assert cl.post(u + "/subtitles", json={"file_id": "x"}).status_code == 400
# rescan: no duplicate
assert db.scan(db.connect(), [root])["added"] == 0 and db.connect().execute("SELECT COUNT(*) FROM films").fetchone()[0] == 1
print("OK test_upgrade_file_api")
