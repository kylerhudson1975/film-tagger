"""Scene photo galleries.

Convention: a folder beside the scene video named '<video file stem> - GALLERY' (canonical). A legacy 'gallery'
subfolder in the video's own folder is linked only when that folder holds exactly one scene video.
Gallery folders are never scanned for catalogue items. Thumbnails are small JPEGs cached under the app data folder."""
import os, re, hashlib

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
SUFFIX = " - GALLERY"
THUMB_PX = 240

def is_gallery_dir(name):
    n = (name or "").strip().lower()
    return n == "gallery" or n.endswith(SUFFIX.lower())

def canonical_dir(video_path):
    return os.path.splitext(video_path)[0] + SUFFIX

def _videos_in(folder, video_ext):
    try: names = os.listdir(folder)
    except OSError: return []
    return [n for n in names if os.path.splitext(n)[1].lower() in video_ext and not re.search(r"\bsample\b", n, re.I)
            and os.path.isfile(os.path.join(folder, n))]

def find(video_path, video_ext):
    """-> (kind, folder) with kind 'canonical' or 'legacy', or (None, None)."""
    if not video_path: return None, None
    cd = canonical_dir(video_path)
    if os.path.isdir(cd): return "canonical", cd
    folder = os.path.dirname(video_path)
    try: subs = [d for d in os.listdir(folder) if d.lower() == "gallery" and os.path.isdir(os.path.join(folder, d))]
    except OSError: subs = []
    if subs and len(_videos_in(folder, video_ext)) == 1:  # one video only, otherwise it's ambiguous whose gallery it is
        return "legacy", os.path.join(folder, subs[0])
    return None, None

def _natkey(s): return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

def images(folder):
    """Sorted image file names directly in the gallery folder."""
    try: names = os.listdir(folder)
    except OSError: return []
    return sorted((n for n in names if os.path.splitext(n)[1].lower() in IMG_EXT and os.path.isfile(os.path.join(folder, n))), key=_natkey)

def safe_name(folder, name):
    """Full path of `name` if it is an image directly inside `folder` (no path tricks), else None."""
    if not name or name != os.path.basename(name) or name in (".", ".."): return None
    p = os.path.join(folder, name)
    return p if os.path.splitext(name)[1].lower() in IMG_EXT and os.path.isfile(p) else None

def thumb(path, cache_dir):
    """Path of a cached small JPEG thumbnail for an image (created on first use), or None if it can't be made."""
    try: st = os.stat(path)
    except OSError: return None
    key = hashlib.sha1(f"{os.path.normcase(path)}|{st.st_size}|{int(st.st_mtime)}|{THUMB_PX}".encode("utf-8")).hexdigest()
    out = os.path.join(cache_dir, key[:2], key + ".jpg")
    if os.path.exists(out): return out
    try:
        import cv2, numpy as np
        with open(path, "rb") as fh: data = fh.read()
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_REDUCED_COLOR_2 if len(data) > 400_000 else cv2.IMREAD_COLOR)
        if img is None: return None
        h, w = img.shape[:2]; s = THUMB_PX / max(h, w)
        if s < 1: img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok: return None
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "wb") as fh: fh.write(buf.tobytes())
        os.replace(tmp, out)
        return out
    except Exception:
        return None
