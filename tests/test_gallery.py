"""Scene photo galleries: detection (canonical + legacy single-video), scan ignores them, thumbs, use as poster,
rename preview/apply moves the gallery. Images are generated test data. Run: python tests/test_gallery.py"""
import os, sys, tempfile, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cv2, numpy as np
import app, db, gallery
cl = app.app.test_client()
root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [root]); db.set_setting(c, "roots", [root]); c.commit()
def touch(p, data=b"x"): os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "wb").write(data)
def jpg(p, w=600, h=900): os.makedirs(os.path.dirname(p), exist_ok=True); cv2.imwrite(p, np.full((h, w, 3), 128, np.uint8))
# A: canonical gallery beside the video (in a folder with other videos)
a = os.path.join(root, "Studio A", "Studio A - 2020-01-02 - P - Title A.mp4"); touch(a); touch(os.path.join(root, "Studio A", "other.mp4"))
for i in (1, 2, 10): jpg(os.path.join(gallery.canonical_dir(a), f"img{i}.jpg"))
touch(os.path.join(gallery.canonical_dir(a), "clip.mp4"))  # a video inside a gallery must not become a scene
# B: legacy 'gallery' subfolder, the folder has exactly one video
b = os.path.join(root, "Studio B", "Scene B", "b.mp4"); touch(b); jpg(os.path.join(root, "Studio B", "Scene B", "gallery", "x.jpg"))
# C: legacy subfolder but two videos -> ambiguous, not linked
c1 = os.path.join(root, "Studio C", "Pack", "c1.mp4"); touch(c1); touch(os.path.join(root, "Studio C", "Pack", "c2.mp4"))
jpg(os.path.join(root, "Studio C", "Pack", "gallery", "y.jpg"))
r = cl.post("/api/scan", json={"roots": [root]}).json
paths = [x[0] for x in db.connect().execute("SELECT path FROM files")]
assert not any("GALLERY" in p or os.sep + "gallery" + os.sep in p for p in paths), paths
assert len(paths) == 5, paths
app.refresh_galleries()
fid = {os.path.basename(p): f for p, f in db.connect().execute("SELECT path, film_id FROM files")}
counts = {k: db.connect().execute("SELECT gallery_count FROM films WHERE id=?", (v,)).fetchone()[0] for k, v in fid.items()}
assert counts[os.path.basename(a)] == 3 and counts["b.mp4"] == 1 and not counts["c1.mp4"] and not counts["other.mp4"], counts
g = cl.get(f"/api/films/{fid[os.path.basename(a)]}/gallery").json
assert g["count"] == 3 and g["images"] == ["img1.jpg", "img2.jpg", "img10.jpg"] and g["kind"] == "canonical"
assert cl.get(f"/api/films/{fid['c1.mp4']}/gallery").status_code == 404
th = cl.get(f"/api/films/{fid[os.path.basename(a)]}/gallery/thumb?name=img1.jpg")
assert th.status_code == 200 and len(th.data) < os.path.getsize(os.path.join(gallery.canonical_dir(a), "img1.jpg")) + 1
assert len(os.listdir(app.GALLERY_THUMBS)) >= 1
assert cl.get(f"/api/films/{fid[os.path.basename(a)]}/gallery/image?name=..%2Ffilms.db").status_code == 404
assert cl.get(f"/api/films/{fid[os.path.basename(a)]}/gallery/image?name=img2.jpg").status_code == 200
# use as poster: numeric updated_at, local custom poster
r = cl.post(f"/api/films/{fid[os.path.basename(a)]}/gallery/poster", json={"name": "img2.jpg"}).json
row = db.connect().execute("SELECT custom_poster, updated_at FROM films WHERE id=?", (fid[os.path.basename(a)],)).fetchone()
assert row[0].startswith("/posters/") and isinstance(row[1], float)
# review-flagged scenes never show the gallery
cc = db.connect(); cc.execute("UPDATE films SET notes='Review by hand' WHERE id=?", (fid["b.mp4"],)); cc.commit()
assert cl.get(f"/api/films/{fid['b.mp4']}/gallery").status_code == 403
cc.execute("UPDATE films SET notes=NULL WHERE id=?", (fid["b.mp4"],))
# rename preview: canonical gallery follows the video; legacy converts to canonical
cc.execute("UPDATE films SET studio='Studio A', release_date='2020-01-02', title='New A', cast_='P' WHERE id=?", (fid[os.path.basename(a)],))
cc.execute("UPDATE films SET studio='Studio B', release_date='2021-05-06', title='Bee', cast_='Q' WHERE id=?", (fid["b.mp4"],)); cc.commit()
pv = cl.get("/api/scenes/rename-preview").json
gal = {os.path.basename(x["old"]): os.path.basename(x["new"]) for x in pv["galleries"]}
assert gal[os.path.basename(gallery.canonical_dir(a))] == "Studio A - 2020-01-02 - P - New A - GALLERY", gal
assert gal["gallery"] == "Studio B - 2021-05-06 - Q - Bee - GALLERY", gal
assert os.path.isdir(gallery.canonical_dir(a))  # preview moves nothing
cl.post("/api/scenes/rename-apply", json={"ids": [fid[os.path.basename(a)], fid["b.mp4"]]})
na = os.path.join(root, "Studio A", "Studio A - 2020-01-02 - P - New A.mp4"); nb = os.path.join(root, "Studio B", "Studio B - 2021-05-06 - Q - Bee.mp4")
assert os.path.isfile(na) and len(gallery.images(gallery.canonical_dir(na))) == 3
assert os.path.isfile(nb) and gallery.images(gallery.canonical_dir(nb)) == ["x.jpg"]
assert not os.path.exists(os.path.join(root, "Studio B", "Scene B"))  # emptied legacy folder removed
assert cl.get(f"/api/films/{fid['b.mp4']}/gallery").json["kind"] == "canonical"
print("ok")
# a gallery added by hand is detected when the scene is opened (GET), without a scan, and updated_at is not bumped
cc = db.connect(); f2 = fid["other.mp4"]; before = cc.execute("SELECT updated_at FROM films WHERE id=?", (f2,)).fetchone()[0]
op = cc.execute("SELECT path FROM files WHERE film_id=?", (f2,)).fetchone()[0]
assert not cl.get(f"/api/films/{f2}").json.get("gallery_count")
jpg(os.path.join(gallery.canonical_dir(op), "z.jpg"), 8, 8)
assert cl.get(f"/api/films/{f2}").json["gallery_count"] == 1
import shutil; shutil.rmtree(gallery.canonical_dir(op))
assert not cl.get(f"/api/films/{f2}").json.get("gallery_count")
assert db.connect().execute("SELECT updated_at FROM films WHERE id=?", (f2,)).fetchone()[0] == before
print("ok (hand-added gallery)")
