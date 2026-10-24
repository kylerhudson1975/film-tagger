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

# Windows MAX_PATH is 260. Keep '<gallery folder>\\<photo file name>' (and gallery-dl's '.part' temp name) well under it:
# the folder may be at most PATH_SAFE - NAME_RESERVE characters; a longer one gets a shortened, stable name.
PATH_SAFE = 240
NAME_RESERVE = 50

def full_dir(video_path):
    """The unshortened '<video stem> - GALLERY' folder (what older versions always used)."""
    return os.path.splitext(video_path)[0] + SUFFIX

def short_dir(video_path, limit=PATH_SAFE - NAME_RESERVE):
    """'<start of the video stem>~<6-char hash> - GALLERY' fitting in `limit` characters; same video path -> same name."""
    stem = os.path.splitext(video_path)[0]; folder, name = os.path.split(stem)
    h = hashlib.sha1(os.path.basename(stem).encode("utf-8")).hexdigest()[:6]
    room = max(10, limit - len(folder) - 1 - len(SUFFIX) - 7)
    return os.path.join(folder, name[:room].rstrip(" .,-") + "~" + h + SUFFIX)

def canonical_dir(video_path):
    """Where a scene's gallery goes: the full name, or short_dir() when the full one would risk MAX_PATH."""
    d = full_dir(video_path)
    return d if len(d) <= PATH_SAFE - NAME_RESERVE else short_dir(video_path)

def _videos_in(folder, video_ext):
    try: names = os.listdir(folder)
    except OSError: return []
    return [n for n in names if os.path.splitext(n)[1].lower() in video_ext and not re.search(r"\bsample\b", n, re.I)
            and os.path.isfile(os.path.join(folder, n))]

def find(video_path, video_ext):
    """-> (kind, folder) with kind 'canonical' or 'legacy', or (None, None)."""
    if not video_path: return None, None
    for cd in dict.fromkeys((full_dir(video_path), canonical_dir(video_path))):  # existing long-named galleries still count
        if os.path.isdir(cd): return "canonical", cd
    folder = os.path.dirname(video_path)
    try: subs = [d for d in os.listdir(folder) if d.lower() == "gallery" and os.path.isdir(os.path.join(folder, d))]
    except OSError: subs = []
    if subs and len(_videos_in(folder, video_ext)) == 1:  # one video only, otherwise it's ambiguous whose gallery it is
        return "legacy", os.path.join(folder, subs[0])
    return None, None

def _natkey(s): return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

def _imgs_in(folder):
    try: names = os.listdir(folder)
    except OSError: return []
    return sorted((n for n in names if not n.startswith(".") and os.path.splitext(n)[1].lower() in IMG_EXT
                   and os.path.isfile(os.path.join(folder, n))), key=_natkey)

def _hidden(path, name):
    """Hidden/system folder (dot-name, Windows hidden/system attribute, $RECYCLE.BIN-style names)."""
    if name.startswith((".", "$", "~")) or name.lower() in ("@eadir", "__macosx", "thumbs", "_thumbs"): return True
    try: a = getattr(os.stat(path), "st_file_attributes", 0)
    except OSError: return True
    return bool(a & 0x6)  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM

def subfolders(folder):
    """Shoot subfolders (one level down) in natural order, hidden/system ones skipped."""
    try: names = os.listdir(folder)
    except OSError: return []
    return sorted((n for n in names if os.path.isdir(os.path.join(folder, n)) and not os.path.islink(os.path.join(folder, n))
                   and not _hidden(os.path.join(folder, n), n)), key=_natkey)

def images(folder):
    """Gallery images: top-level names first, then 'Sub/name' for each shoot subfolder (one level down), natural order."""
    out = _imgs_in(folder)
    for d in subfolders(folder): out += [d + "/" + n for n in _imgs_in(os.path.join(folder, d))]
    return out

def groups(names):
    """[{'folder': None or subfolder name, 'start': index of its first image, 'count': n}] in listing order."""
    out = []
    for i, n in enumerate(names):
        g = n.split("/", 1)[0] if "/" in n else None
        if not out or out[-1]["folder"] != g: out.append(dict(folder=g, start=i, count=0))
        out[-1]["count"] += 1
    return out

def safe_name(folder, name):
    """Full path of an image 'name' or 'Sub/name' (one level down) inside `folder`, or None for anything else
    (absolute paths, '..', drive letters, deeper paths, hidden subfolders, non-images, paths resolving outside)."""
    if not name or not isinstance(name, str) or "\x00" in name or ":" in name: return None
    parts = name.replace("\\", "/").split("/")
    if len(parts) > 2 or any(p in ("", ".", "..") or p != p.strip() and not p.strip() for p in parts): return None
    if os.path.isabs(name) or name.startswith(("/", "\\")): return None
    if len(parts) == 2 and _hidden(os.path.join(folder, parts[0]), parts[0]): return None
    if os.path.splitext(parts[-1])[1].lower() not in IMG_EXT: return None
    p = os.path.join(folder, *parts)
    root = os.path.normcase(os.path.realpath(folder)); rp = os.path.normcase(os.path.realpath(p))
    if os.path.dirname(rp) != root and os.path.dirname(os.path.dirname(rp)) != root: return None
    return p if os.path.isfile(p) else None

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
