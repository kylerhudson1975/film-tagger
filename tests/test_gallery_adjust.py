"""Gallery photo adjustments: synthetic images only. Run: python tests/test_gallery_adjust.py"""
import os, sys, json, tempfile, shutil, struct, zlib
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageChops, ImageStat
import galleryadjust as ga
import gallery as gallerymod
import app, db, people

def tmpimg(*a, **k):
    p = os.path.join(tempfile.mkdtemp() if not a else a[0], k.pop("name", "photo.jpg"))
    return p

# --- helpers: a known synthetic RGB photo (gradient + shapes, no people) ---
def make_photo(path, size=(80, 60), mode="RGB", fmt=None):
    im = Image.new("RGB", size)
    px = im.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 255) // max(1, w - 1), (y * 255) // max(1, h - 1), 120)
    draw = ImageDraw.Draw(im)
    draw.rectangle([10, 10, 30, 30], fill=(240, 20, 20))
    draw.ellipse([40, 20, 70, 50], fill=(20, 200, 40))
    if mode != "RGB":
        im = im.convert(mode)
    ext = os.path.splitext(path)[1].lower()
    kw = {}
    if ext in (".jpg", ".jpeg"):
        kw["quality"] = 95
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    im.save(path, **kw)
    return path

# 1. identity (1,1,1,0) is a pixel-level no-op
src = Image.open(make_photo(os.path.join(d, "id.png")))
out = ga.apply_adjustments(src.copy(), 1, 1, 1, 0)
assert list(src.getdata()) == list(out.getdata()), "identity must not change pixels"

# 2. contrast > 1 increases spread from the mean
lo = make_photo(os.path.join(d, "c.png"))
im = Image.open(lo)
hi = ga.apply_adjustments(im.copy(), contrast=1.5)
def spread(im):
    mean = ImageStat.Stat(im.convert("L")).mean[0]
    return sum(abs(p[0] - mean) + abs(p[1] - mean) + abs(p[2] - mean) for p in im.getdata())
assert spread(hi) > spread(im) * 1.1, "contrast 1.5 should increase contrast"

# 3. brightness > 1 raises the mean
br = ga.apply_adjustments(im.copy(), brightness=1.3)
assert ImageStat.Stat(br).mean[0] > ImageStat.Stat(im).mean[0] + 5, "brightness 1.3 should raise mean"

# 4. gamma LUT is the documented formula (mid gray, gamma 2 -> brighter)
mid = Image.new("RGB", (8, 8), (128, 128, 128))
g2 = ga.apply_gamma(mid, 2.0)
got = g2.getpixel((0, 0))[0]
expect = min(255, max(0, int(round((128 / 255.0) ** (1 / 2.0) * 255))))
assert got == expect, (got, expect)
g1 = ga.apply_gamma(mid, 1.0)
assert list(mid.getdata()) == list(g1.getdata())

# 5. sharpen > 0 changes edge pixels (a hard box on gray)
box = Image.new("RGB", (40, 40), (80, 80, 80))
ImageDraw.Draw(box).rectangle([10, 10, 29, 29], fill=(200, 200, 200))
sh = ga.apply_adjustments(box.copy(), sharpen=1.5)
assert list(box.getdata()) != list(sh.getdata()), "sharpen should change the image"
# and it matches Pillow UnsharpMask percent=150
ref = box.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
assert list(sh.getdata()) == list(ref.getdata()), "sharpen 1.5 should be UnsharpMask percent=150"

# 6. parse_params rejects bad values, defaults missing ones
p = ga.parse_params({})
assert p == ga.DEFAULTS
try:
    ga.parse_params({"contrast": 99})
    raise SystemExit("range not enforced")
except ValueError:
    pass
try:
    ga.parse_params({"gamma": "nope"})
    raise SystemExit("bad number not caught")
except ValueError:
    pass

# 7. backup is created once; a second save does not overwrite the original
folder = os.path.join(d, "gal")
os.makedirs(folder)
photo = make_photo(os.path.join(folder, "shot.jpg"))
original_bytes = open(photo, "rb").read()
r, err = ga.adjust(photo, {"contrast": 1.2, "brightness": 1.05})
assert err is None and r["ok"] and r["has_original"] and not r["unchanged"]
bak = ga.original_path(photo)
assert os.path.isfile(bak) and open(bak, "rb").read() == original_bytes
# _originals is hidden from gallery listings
assert "shot.jpg" in gallerymod.images(folder)
assert all(not n.lower().startswith("_originals") and "/_originals" not in n.lower() for n in gallerymod.images(folder))
assert gallerymod.safe_name(folder, "_originals/shot.jpg") is None
# second edit must not replace the first backup
after_first = open(photo, "rb").read()
assert after_first != original_bytes
r2, err = ga.adjust(photo, {"gamma": 0.8})
assert err is None
assert open(bak, "rb").read() == original_bytes, "original must be kept once"

# 8. revert restores the exact original bytes; backup stays
r3, err = ga.revert(photo)
assert err is None and r3["reverted"]
assert open(photo, "rb").read() == original_bytes
assert os.path.isfile(bak) and open(bak, "rb").read() == original_bytes
# revert with no backup
lonely = make_photo(os.path.join(folder, "plain.png"))
r4, err = ga.revert(lonely)
assert err and "No original" in err

# 9. identity save is a no-op and does not create a backup
plain2 = make_photo(os.path.join(folder, "plain2.jpg"))
before = open(plain2, "rb").read()
r5, err = ga.adjust(plain2, {"contrast": 1, "brightness": 1, "gamma": 1, "sharpen": 0})
assert err is None and r5["unchanged"] and not r5["has_original"]
assert open(plain2, "rb").read() == before
assert not os.path.isfile(ga.original_path(plain2))

# 10. PNG alpha is preserved (contrast/brightness/gamma do not punch holes in it)
rgba_path = os.path.join(folder, "alpha.png")
rgba = Image.new("RGBA", (16, 16), (10, 20, 30, 0))
ImageDraw.Draw(rgba).rectangle([2, 2, 8, 8], fill=(200, 100, 50, 255))
rgba.save(rgba_path)
im_a = ga.open_image(rgba_path)
out_a = ga.apply_adjustments(im_a, contrast=1.4, brightness=1.1, gamma=0.9)
assert out_a.mode == "RGBA"
# transparent pixels stay fully transparent
assert out_a.getpixel((0, 0))[3] == 0
assert out_a.getpixel((5, 5))[3] == 255

# 11. JPEG format is kept
assert open(photo, "rb").read()[:2] == b"\xff\xd8"  # after revert it's jpeg
ga.adjust(photo, {"contrast": 1.1})
assert open(photo, "rb").read()[:2] == b"\xff\xd8"

# --- API: person gallery, synthetic folder (never Kyle's photos) ---
cl = app.app.test_client()
c = db.connect(); people.migrate(c)
c.execute("INSERT INTO people(name,name_norm,source) VALUES('Test Person','test person','manual')")
c.commit()
pid = c.execute("SELECT id FROM people WHERE name_norm='test person'").fetchone()[0]
api_folder = os.path.join(d, "person_gal")
os.makedirs(api_folder)
make_photo(os.path.join(api_folder, "a.jpg"))
make_photo(os.path.join(api_folder, "b.png"))
# a subfolder photo
os.makedirs(os.path.join(api_folder, "set1"))
make_photo(os.path.join(api_folder, "set1", "c.jpg"))
cl.post(f"/api/person/{pid}/gallery/folder", json={"path": api_folder})

listing = cl.get(f"/api/person/{pid}/gallery").json
assert "a.jpg" in listing["images"] and "b.png" in listing["images"] and "set1/c.jpg" in listing["images"]

info = cl.get(f"/api/person/{pid}/gallery/adjust", query_string={"name": "a.jpg"}).json
assert info["has_original"] is False and info["width"] == 80 and info["height"] == 60

orig = open(os.path.join(api_folder, "a.jpg"), "rb").read()
sv = cl.post(f"/api/person/{pid}/gallery/adjust", json={"name": "a.jpg", "contrast": 1.25, "brightness": 1.05, "gamma": 0.9, "sharpen": 0.5})
assert sv.status_code == 200 and sv.json["ok"] and sv.json["has_original"] and sv.json.get("v")
assert open(os.path.join(api_folder, "_originals", "a.jpg"), "rb").read() == orig
assert open(os.path.join(api_folder, "a.jpg"), "rb").read() != orig
# listing still does not include the backup
listing = cl.get(f"/api/person/{pid}/gallery").json
assert all("_originals" not in n for n in listing["images"])

# bad name / missing
assert cl.get(f"/api/person/{pid}/gallery/adjust", query_string={"name": "_originals/a.jpg"}).status_code == 404
assert cl.post(f"/api/person/{pid}/gallery/adjust", json={"name": "nope.jpg", "contrast": 1.2}).status_code == 404
assert cl.post(f"/api/person/{pid}/gallery/adjust", json={"name": "a.jpg", "contrast": 50}).status_code == 400

# revert via API
rv = cl.post(f"/api/person/{pid}/gallery/adjust/revert", json={"name": "a.jpg"})
assert rv.status_code == 200 and rv.json["reverted"]
assert open(os.path.join(api_folder, "a.jpg"), "rb").read() == orig
# backup still there
assert os.path.isfile(os.path.join(api_folder, "_originals", "a.jpg"))
# revert on a photo with no backup
assert cl.post(f"/api/person/{pid}/gallery/adjust/revert", json={"name": "b.png"}).status_code == 404

# subfolder photo works
sv2 = cl.post(f"/api/person/{pid}/gallery/adjust", json={"name": "set1/c.jpg", "contrast": 1.2})
assert sv2.status_code == 200 and os.path.isfile(os.path.join(api_folder, "set1", "_originals", "c.jpg"))

print("ok")
