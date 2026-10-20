import os, sys, tempfile, shutil
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
mov, sc = os.path.join(d, "Movies"), os.path.join(d, "Scenes")
for x in (os.path.join(mov, "A (2000)"), os.path.join(sc, "Sis Loves Me")): os.makedirs(x)
def mk(p, seed):
    with open(p, "wb") as f: f.write(os.urandom(200_000) if seed is None else bytes([seed]) * 200_000)
a = os.path.join(mov, "A (2000)", "A (2000).mkv"); mk(a, None)
s1 = os.path.join(sc, "Sis Loves Me", "SisLovesMe Surprise Visit Tristan Summers.mp4"); mk(s1, 7)
open(os.path.splitext(s1)[0] + ".srt", "w").write("1")
c = db.connect(); db.set_setting(c, "roots", [mov, sc]); db.set_setting(c, "scene_roots", [sc]); db.set_setting(c, "tpdb_key", "t"); c.commit()
assert db.scan(c, [mov, sc])["added"] == 2
cl = app.app.test_client()
fa = c.execute("SELECT film_id FROM files WHERE path=?", (a,)).fetchone()[0]
cl.delete(f"/api/films/{fa}")
assert os.path.exists(a) and not c.execute("SELECT 1 FROM films WHERE id=?", (fa,)).fetchone()
assert db.scan(c, [mov, sc])["added"] == 0  # same path: skipped
b = os.path.join(mov, "A (2000)", "Renamed copy.mkv"); os.rename(a, b)
assert db.scan(c, [mov, sc])["added"] == 0  # renamed: matched by size + hash
items = cl.get("/api/excluded").json["items"]; assert len(items) == 1 and items[0]["hashed"]
os.rename(b, a); assert cl.post(f"/api/excluded/{items[0]['id']}/restore").json["added"] is True
assert c.execute("SELECT 1 FROM files WHERE path=?", (a,)).fetchone()
# Use / Load applies the match and renames the scene file with its sidecar
SC = {"id": "u1", "title": "Bone Voyage", "date": "2021-09-10", "site": {"name": "Sis Loves Me"}, "performers": [{"name": "Tristan Summers"}]}
tpdb.call = tpdb.call_retry = lambda path, tok, **k: {"data": SC}
fs = c.execute("SELECT film_id FROM files WHERE path=?", (s1,)).fetchone()[0]
r = cl.post(f"/api/films/{fs}/tpdb", json={"tpdb_id": "u1"}).json
new = os.path.join(sc, "Sis Loves Me", "Sis Loves Me - 2021-09-10 - Tristan Summers - Bone Voyage.mp4")
assert r["files"][0]["path"] == new and os.path.exists(new) and os.path.exists(new[:-4] + ".srt") and not os.path.exists(s1), r["files"]
assert open(os.path.join(sc, "_filmtagger_renames.csv"), encoding="utf-8-sig").read().count("Bone Voyage") == 2
assert db.scan(c, [mov, sc])["missing_files"] == 0
shutil.rmtree(d); print("ok")
