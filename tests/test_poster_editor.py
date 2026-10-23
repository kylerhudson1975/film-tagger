"""Poster editor: crop / rotate / flip done with OpenCV on the full-size original, result size readout, the previous
poster backed up to posters/backup/ and put back by Restore previous, and the hand-edit flag. Synthetic images only,
temp DB, no network. Run: python tests/test_poster_editor.py"""
import json, os, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import cv2, numpy as np
import db, app as A
A.kick_focus = lambda: None
cl = A.app.test_client()

def quad(w, h):
    """w x h image with four coloured quadrants: TL red, TR green, BL blue, BR white (BGR)."""
    im = np.zeros((h, w, 3), np.uint8); hw, hh = w // 2, h // 2
    im[:hh, :hw] = (0, 0, 255); im[:hh, hw:] = (0, 255, 0); im[hh:, :hw] = (255, 0, 0); im[hh:, hw:] = (255, 255, 255)
    return im
def px(im, x, y): return tuple(int(v) for v in im[y, x][:3])
def near(a, b, tol=40): return all(abs(p - q) <= tol for p, q in zip(a, b))
RED, GREEN, BLUE, WHITE = (0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 255)

# --- pure helpers: crop math, rotate/flip, size readout ---
q = quad(100, 60)
r = A.pe_apply(q, 0, False, {"x": 10, "y": 5, "w": 30, "h": 20}); assert r.shape[:2] == (20, 30) and near(px(r, 0, 0), RED)
r = A.pe_apply(q, 0, False, {"x": 90, "y": 50, "w": 500, "h": 500}); assert r.shape[:2] == (10, 10)  # clamped inside
r = A.pe_apply(q, 0, False, {"x": -20, "y": -20, "w": 30, "h": 30}); assert r.shape[:2] == (30, 30)
assert A.pe_clamp_crop(None, 100, 60) == (0, 0, 100, 60)
r = A.pe_apply(q, 1, False); assert r.shape[:2] == (100, 60) and near(px(r, 0, 0), BLUE) and near(px(r, 59, 0), RED)   # right: TL<-BL
r = A.pe_apply(q, -1, False); assert r.shape[:2] == (100, 60) and near(px(r, 0, 0), GREEN)                             # left: TL<-TR
r = A.pe_apply(q, 2, False); assert near(px(r, 0, 0), WHITE)
r = A.pe_apply(q, 0, True); assert near(px(r, 0, 0), GREEN) and near(px(r, 99, 0), RED)
r = A.pe_apply(q, 1, True); assert near(px(r, 0, 0), RED)                                                              # rotate, then flip
assert A.pe_result_size(1000, 1500, 0, {"x": 0, "y": 0, "w": 500, "h": 750}) == ((500, 750), (500, 750))
assert A.pe_result_size(1000, 1500, 1, None) == ((1500, 1000), (1500, 1000))
assert A.pe_result_size(4000, 6000, 0, None) == ((4000, 6000), (1333, 2000))  # only huge results are shrunk
big = A.pe_apply(np.zeros((6000, 4000, 3), np.uint8), 0, False); assert big.shape[:2] == (2000, 1333)

# --- endpoints on a temporary film with a synthetic local poster ---
c = db.connect(); now = time.time()
c.execute("INSERT INTO films(title, kind, edited_fields, updated_at) VALUES('Synthetic', 'movie', '[]', ?)", (now,))
fid = c.execute("SELECT id FROM films WHERE title='Synthetic'").fetchone()[0]; c.commit()
assert cl.get(f"/api/films/{fid}/posteredit").status_code == 400  # no poster yet
src = quad(1200, 1800); ok, enc = cv2.imencode(".jpg", src, [cv2.IMWRITE_JPEG_QUALITY, 98])
r = cl.post(f"/api/films/{fid}/poster", data={"file": (__import__("io").BytesIO(enc.tobytes()), "p.jpg", "image/jpeg")}, content_type="multipart/form-data")
assert r.status_code == 200, r.json
orig = r.json["custom_poster"]; orig_bytes = open(os.path.join(A.POSTERS_DIR, os.path.basename(orig)), "rb").read()
i = cl.get(f"/api/films/{fid}/posteredit").json
assert (i["width"], i["height"], i["ext"], i["can_restore"]) == (1200, 1800, "jpg", False), i
assert cl.get(i["image"]).data == orig_bytes  # the editor gets the full-size original
r = cl.post(f"/api/films/{fid}/posteredit", json={"rot": 0, "flip": False, "crop": {"x": 600, "y": 0, "w": 400, "h": 600}})
assert r.status_code == 200, r.json
new = r.json["custom_poster"]; assert new != orig and new.endswith(".jpg") and r.json["can_restore"]
im = cv2.imread(os.path.join(A.POSTERS_DIR, os.path.basename(new))); assert im.shape[:2] == (600, 400) and near(px(im, 200, 300), GREEN)
assert "custom_poster" in json.loads(db.connect().execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()[0])
bk = os.path.join(A.POSTER_BACKUP_DIR, os.path.basename(orig)); assert open(bk, "rb").read() == orig_bytes  # old poster kept
assert not os.path.exists(os.path.join(A.POSTERS_DIR, os.path.basename(orig)))
assert cl.get(f"/api/films/{fid}/posteredit").json["can_restore"] is True
# Restore previous after an edit brings the original back byte-for-byte; the edited one is backed up
r = cl.post(f"/api/films/{fid}/findposters/restore", json={}); assert r.status_code == 200, r.json
assert r.json["custom_poster"] == orig and open(os.path.join(A.POSTERS_DIR, os.path.basename(orig)), "rb").read() == orig_bytes
assert os.path.exists(os.path.join(A.POSTER_BACKUP_DIR, os.path.basename(new)))
# rotate + flip on the server match the helpers
r = cl.post(f"/api/films/{fid}/posteredit", json={"rot": 1, "flip": True}); assert r.status_code == 200
im = cv2.imread(os.path.join(A.POSTERS_DIR, os.path.basename(r.json["custom_poster"])))
assert im.shape[:2] == (1200, 1800) and near(px(im, 10, 10), RED) and near(px(im, 1790, 10), BLUE)
# PNG source stays PNG
ok, pn = cv2.imencode(".png", quad(300, 450))
cl.post(f"/api/films/{fid}/poster", data={"file": (__import__("io").BytesIO(pn.tobytes()), "p.png", "image/png")}, content_type="multipart/form-data")
r = cl.post(f"/api/films/{fid}/posteredit", json={"crop": {"x": 0, "y": 0, "w": 150, "h": 225}})
assert r.status_code == 200 and r.json["custom_poster"].endswith(".png")
assert cv2.imread(os.path.join(A.POSTERS_DIR, os.path.basename(r.json["custom_poster"]))).shape[:2] == (225, 150)
# bad input, missing film, review-flagged scene
assert cl.post(f"/api/films/{fid}/posteredit", json={"crop": {"x": "a"}}).status_code == 400
assert cl.post("/api/films/999999/posteredit", json={}).status_code == 404
c = db.connect(); c.execute("UPDATE films SET notes='Review by hand' WHERE id=?", (fid,)); c.commit()
if A.is_review(c, fid): assert cl.post(f"/api/films/{fid}/posteredit", json={}).status_code == 403
print("OK test_poster_editor")
