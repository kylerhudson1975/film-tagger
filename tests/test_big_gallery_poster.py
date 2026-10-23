"""Big gallery photos (> 15 MB) used as poster are downscaled to a JPEG copy; small ones are kept as-is; originals are
untouched; review-flagged scenes stay blocked; absurd inputs are refused. Images are generated noise.
Run: python tests/test_big_gallery_poster.py"""
import os, sys, tempfile, hashlib
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cv2, numpy as np
import app, db, gallery
cl = app.app.test_client()
root = tempfile.mkdtemp(); c = db.connect(); db.set_setting(c, "scene_roots", [root]); db.set_setting(c, "roots", [root]); c.commit()
v = os.path.join(root, "Studio", "Studio - 2020-01-02 - P - Big.mp4"); os.makedirs(os.path.dirname(v)); open(v, "wb").write(b"x")
gd = gallery.canonical_dir(v); os.makedirs(gd)
rng = np.random.default_rng(1)
big = os.path.join(gd, "big.jpg"); cv2.imwrite(big, rng.integers(0, 256, (4480, 6720, 3), np.uint8), [cv2.IMWRITE_JPEG_QUALITY, 100])
small = os.path.join(gd, "small.jpg"); cv2.imwrite(small, np.full((900, 600, 3), 90, np.uint8))
assert os.path.getsize(big) > app.POSTER_MAX, os.path.getsize(big)
h0 = hashlib.sha256(open(big, "rb").read()).hexdigest(); m0 = os.path.getmtime(big)
cl.post("/api/scan", json={"roots": [root]}); app.refresh_galleries()
fid = db.connect().execute("SELECT film_id FROM files").fetchone()[0]
def poster_file(r):
    return os.path.join(app.POSTERS_DIR, os.path.basename(r.json["custom_poster"]))
r = cl.post(f"/api/films/{fid}/gallery/poster", json={"name": "big.jpg"})
assert r.status_code == 200, (r.status_code, r.json)
p = poster_file(r); assert p.endswith(".jpg") and os.path.getsize(p) <= app.POSTER_MAX
im = cv2.imread(p); assert im.shape[:2] == (1333, 2000), im.shape  # 6720x4480 -> 2000x1333, aspect kept
assert hashlib.sha256(open(big, "rb").read()).hexdigest() == h0 and os.path.getmtime(big) == m0  # original untouched
r = cl.post(f"/api/films/{fid}/gallery/poster", json={"name": "small.jpg"})
assert r.status_code == 200 and open(poster_file(r), "rb").read() == open(small, "rb").read()  # normal image kept as-is
# upload path also shrinks big files
r = cl.post(f"/api/films/{fid}/poster", data={"file": (open(big, "rb"), "big.jpg", "image/jpeg")}, content_type="multipart/form-data")
assert r.status_code == 200 and cv2.imread(poster_file(r)).shape[:2] == (1333, 2000), (r.status_code, r.json)
# absurd inputs: over the byte cap / not an image
assert app.fit_poster(b"\0" * (app.POSTER_SRC_MAX + 1))[0] is None
assert app.fit_poster(b"\xff\xd8\xff" + b"\0" * 100)[0] is None
old = app.POSTER_SRC_PIXELS; app.POSTER_SRC_PIXELS = 1000
assert app.fit_poster(open(big, "rb").read())[0] is None; app.POSTER_SRC_PIXELS = old
# review-flagged scenes stay blocked
c = db.connect(); c.execute("UPDATE films SET notes='Review by hand' WHERE id=?", (fid,)); c.commit()
assert cl.post(f"/api/films/{fid}/gallery/poster", json={"name": "big.jpg"}).status_code == 403
print("OK test_big_gallery_poster")
