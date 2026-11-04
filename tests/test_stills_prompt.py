"""Make stills popup backend: /api/films/<id>/stills/info (existing images, count choices), POST {count 10|20|40,
mode add|replace}: ask when the folder has images, Replace moves them into a hidden backup subfolder (never deletes),
Add numbers new stills after the old ones; Review by hand stays blocked. Then a real run (ffmpeg test pattern, not a
real video) makes exactly 10 stills. Images are generated test data. Run: python tests/test_stills_prompt.py"""
import os, sys, tempfile, time, shutil, subprocess
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, gallery, stills
cl = app.app.test_client(); root = tempfile.mkdtemp(); c = db.connect()
def touch(p, data=b"x"): os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data)
v = os.path.join(root, "Movie (2001).mp4"); touch(v); v2 = os.path.join(root, "Other (2002).mp4"); touch(v2)
c.execute("INSERT INTO films(id,title) VALUES(1,'Movie')"); c.execute("INSERT INTO files(film_id,path) VALUES(1,?)", (v,))
c.execute("INSERT INTO films(id,title,notes) VALUES(2,'Other','Review by hand: x')"); c.execute("INSERT INTO files(film_id,path) VALUES(2,?)", (v2,)); c.commit()
calls = []; real_start = stills.start
stills.start = lambda fid, video, dest, done=None, count=20, start_no=1: calls.append((fid, dest, count, start_no))
i = cl.get("/api/films/1/stills/info").json
assert i["existing"] == 0 and i["counts"] == [10, 20, 40] and i["default"] == 20, i
assert cl.get("/api/films/2/stills/info").status_code == 403 and cl.post("/api/films/2/stills", json={"count": 20}).status_code == 403
assert cl.post("/api/films/1/stills", json={"count": 15}).status_code == 400
r = cl.post("/api/films/1/stills", json={"count": 10}).json; assert r["ok"] and r["count"] == 10 and r["backup"] is None
dest = gallery.canonical_dir(v); assert calls[-1] == (1, dest, 10, 1), calls
# the folder now has stills 01-03 + a photo + a shoot subfolder
for n in ("still 01 (00h00m05s).jpg", "still 02 (00h01m00s).jpg", "still 03 (00h02m00s).jpg", "photo.jpg"): touch(os.path.join(dest, n))
touch(os.path.join(dest, "Shoot", "a.jpg"))
assert cl.get("/api/films/1/stills/info").json["existing"] == 5
r = cl.post("/api/films/1/stills", json={"count": 20}); assert r.status_code == 409 and r.json["confirm"] and r.json["existing"] == 5
r = cl.post("/api/films/1/stills", json={"count": 40, "mode": "add"}).json
assert calls[-1] == (1, dest, 40, 4) and r["backup"] is None and len(os.listdir(dest)) == 5, (calls, os.listdir(dest))
r = cl.post("/api/films/1/stills", json={"count": 20, "overwrite": True}).json; assert calls[-1][3] == 4  # older clients: add
r = cl.post("/api/films/1/stills", json={"count": 20, "mode": "replace"}).json
bk = r["backup"]; assert bk and bk.startswith(".stills-backup-"), r
assert sorted(os.listdir(dest)) == [bk] and sorted(os.listdir(os.path.join(dest, bk))) == ["Shoot", "photo.jpg", "still 01 (00h00m05s).jpg", "still 02 (00h01m00s).jpg", "still 03 (00h02m00s).jpg"]
assert calls[-1] == (1, dest, 20, 1) and gallery.images(dest) == [] and cl.get("/api/films/1/stills/info").json["existing"] == 0
stills.JOB["running"] = True; assert cl.post("/api/films/1/stills", json={"count": 20}).status_code == 409; stills.JOB["running"] = False
# real run on a generated test pattern: 10 stills, numbered after an existing one, nothing removed
stills.start = real_start
if shutil.which("ffmpeg"):
    os.remove(v); subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=60", "-pix_fmt", "yuv420p", v], check=True)
    touch(os.path.join(dest, "still 07 (00h00m01s).jpg"))
    assert cl.post("/api/films/1/stills", json={"count": 10, "mode": "add"}).json["ok"]
    t = time.time()
    while stills.JOB["running"] and time.time() - t < 300: time.sleep(0.5)
    j = cl.get("/api/stills/status").json; assert not j["error"] and j["saved"] == 10, j
    made = sorted(n for n in os.listdir(dest) if n.startswith("still "))
    assert len(made) == 11 and made[0].startswith("still 07") and made[1].startswith("still 08") and made[-1].startswith("still 17"), made
    assert os.path.isdir(os.path.join(dest, bk))
else: print("(ffmpeg not on PATH: real run skipped)")
print("ok")
