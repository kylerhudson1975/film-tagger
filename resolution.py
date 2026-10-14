"""Video resolution detection for Film Tagger (pure Python, works in embeddable Python 3.12).

Order: ffprobe (if on PATH) -> built-in header readers (MP4/MOV/M4V, MKV/WebM, AVI, MPEG-1/2 PS/TS/VOB)
-> filename tokens (2160p, 4K, 1080p, 720p, DVDRip ...). Never raises; returns None when unknown."""
import os, re, shutil, struct, subprocess, json

CHOICES = ["SD", "480p", "576p", "720p", "1080p", "1440p", "4K"]
RANK = {v: i for i, v in enumerate(CHOICES)}

def bucket(w, h):
    """Bucket real dimensions. Width is considered too so letterboxed/scope encodes (1920x800) land correctly."""
    w, h = int(w or 0), int(h or 0)
    if h <= 0: return None
    if h >= 2000 or w >= 3200: return "4K"
    if h >= 1300 or w >= 2200: return "1440p"
    if h >= 900 or w >= 1600: return "1080p"
    if h >= 650 or w >= 1100: return "720p"
    if h >= 560: return "576p"
    if h >= 440: return "480p"
    return "SD"

def normalize(v):
    """Map user/legacy spellings to a CHOICES value ('' -> None)."""
    s = (v or "").strip()
    if not s: return None
    l = s.lower().replace(" ", "")
    if l in ("4k", "2160p", "2160p/4k", "uhd", "2160"): return "4K"
    for c in CHOICES:
        if l == c.lower() or l == c.lower().rstrip("p"): return c
    raise ValueError(f"Unknown resolution: {s}")

_TOK = [(r"2160[pi]|4k|uhd", "4K"), (r"1440p", "1440p"), (r"1080[pi]|fullhd|fhd", "1080p"), (r"720p", "720p"),
        (r"576[pi]", "576p"), (r"480[pi]", "480p"), (r"dvd[- ]?rip|dvdscr|dvd5|dvd9|vhs[- ]?rip|sdtv|pdtv|tvrip|vhs|xvid|divx", "SD")]

def guess_from_name(path):
    name = re.split(r"[\\/]", path or "")[-1]
    for pat, v in _TOK:
        if re.search(r"(?<![a-z0-9])(?:" + pat + r")(?![a-z0-9])", name, re.I): return v
    return None

# ---------- ffprobe ----------
_FF = None
def ffprobe_path():
    global _FF
    if _FF is None:
        _FF = shutil.which("ffprobe") or ""
        if not _FF:
            here = os.path.dirname(os.path.abspath(__file__))
            for cand in (os.path.join(here, "ffprobe.exe"), os.path.join(here, "bin", "ffprobe.exe")):
                if os.path.isfile(cand): _FF = cand; break
        if not _FF and os.name == "nt":  # winget installs may not be on the PATH a background pythonw inherits
            import glob
            la = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
            hits = glob.glob(os.path.join(la, "Microsoft", "WinGet", "Links", "ffprobe.exe")) + \
                   glob.glob(os.path.join(la, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin", "ffprobe.exe")) + \
                   glob.glob(r"C:\ffmpeg\bin\ffprobe.exe") + glob.glob(r"C:\Program Files\ffmpeg\bin\ffprobe.exe")
            if hits: _FF = hits[0]
    return _FF

def probe_ffprobe(path):
    ff = ffprobe_path()
    if not ff: return None
    try:
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW under pythonw
        out = subprocess.run([ff, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                              "-of", "json", path], capture_output=True, timeout=60, **kw).stdout
        s = (json.loads(out or b"{}").get("streams") or [{}])[0]
        if s.get("width") and s.get("height"): return int(s["width"]), int(s["height"])
    except Exception: pass
    return None

# ---------- MP4 / MOV ----------
def _boxes(f, start, end):
    pos = start
    while pos + 8 <= end:
        f.seek(pos); hdr = f.read(8)
        if len(hdr) < 8: return
        size, typ = struct.unpack(">I4s", hdr); hl = 8
        if size == 1: size = struct.unpack(">Q", f.read(8))[0]; hl = 16
        elif size == 0: size = end - pos
        if size < hl: return
        yield typ, pos + hl, pos + size
        pos += size

def probe_mp4(f, fsize):
    best = None
    for typ, s, e in _boxes(f, 0, fsize):
        if typ != b"moov": continue
        for t2, s2, e2 in _boxes(f, s, e):
            if t2 != b"trak": continue
            wh, vide = None, False
            for t3, s3, e3 in _boxes(f, s2, e2):
                if t3 == b"tkhd":
                    f.seek(s3); d = f.read(e3 - s3)
                    if len(d) >= 84: wh = (struct.unpack(">I", d[-8:-4])[0] >> 16, struct.unpack(">I", d[-4:])[0] >> 16)
                elif t3 == b"mdia":
                    for t4, s4, e4 in _boxes(f, s3, e3):
                        if t4 == b"hdlr": f.seek(s4 + 8); vide = f.read(4) == b"vide"
            if wh and wh[0] and wh[1] and (vide or best is None):
                if vide: return wh
                best = wh
        break
    return best

# ---------- Matroska / WebM ----------
def _vint(f, keep_marker=False):
    b = f.read(1)
    if not b: return None, 0
    b0 = b[0]; n = 1; mask = 0x80
    while n <= 8 and not (b0 & mask): n += 1; mask >>= 1
    if n > 8: return None, 0
    v = b0 if keep_marker else (b0 & (mask - 1))
    rest = f.read(n - 1)
    if len(rest) < n - 1: return None, 0
    for x in rest: v = (v << 8) | x
    unknown = (not keep_marker) and v == (1 << (7 * n)) - 1
    return (-1 if unknown else v), n

def probe_mkv(f, fsize):
    f.seek(0)
    eid, _ = _vint(f, True)
    if eid != 0x1A45DFA3: return None
    sz, _ = _vint(f); f.seek(f.tell() + sz)
    eid, _ = _vint(f, True)
    if eid != 0x18538067: return None
    ssz, _ = _vint(f); pos = f.tell(); end = fsize if ssz < 0 else min(fsize, pos + ssz)
    def children(p, e, limit=200000):
        n = 0
        while p < e and n < limit:
            f.seek(p); i, _ = _vint(f, True); s, _ = _vint(f)
            if i is None or s is None: return
            d = f.tell(); yield i, d, (e if s < 0 else d + s); n += 1
            if s < 0: return
            p = d + s
    for i, d, e in children(pos, end):
        if i == 0x1654AE6B:  # Tracks
            for ti, td, te in children(d, e):
                if ti != 0xAE: continue  # TrackEntry
                for vi, vd, ve in children(td, te):
                    if vi != 0xE0: continue  # Video
                    w = h = 0
                    for pi, pd, pe in children(vd, ve):
                        if pi in (0xB0, 0xBA):
                            f.seek(pd); v = int.from_bytes(f.read(pe - pd), "big")
                            if pi == 0xB0: w = v
                            else: h = v
                    if w and h: return w, h
            return None
    return None

# ---------- AVI ----------
def probe_avi(f, fsize):
    f.seek(0); d = f.read(4096)
    if d[:4] != b"RIFF" or d[8:12] not in (b"AVI ", b"AVIX"): return None
    i = d.find(b"avih")
    if i < 0 or i + 48 > len(d): return None
    w, h = struct.unpack("<II", d[i + 8 + 32:i + 8 + 40])
    return (w, h) if w and h else None

# ---------- MPEG-1/2 sequence header (mpg, vob, ts, m2ts with MPEG-2 video) ----------
def probe_mpeg(f, fsize):
    f.seek(0); d = f.read(4 * 1024 * 1024)
    i = d.find(b"\x00\x00\x01\xb3")
    if i < 0 or i + 7 > len(d): return None
    a, b, c = d[i + 4], d[i + 5], d[i + 6]
    w, h = (a << 4) | (b >> 4), ((b & 0x0F) << 8) | c
    return (w, h) if 16 <= w <= 4096 and 16 <= h <= 4096 else None

READERS = {".mp4": probe_mp4, ".m4v": probe_mp4, ".mov": probe_mp4, ".3gp": probe_mp4,
           ".mkv": probe_mkv, ".webm": probe_mkv, ".avi": probe_avi, ".divx": probe_avi,
           ".mpg": probe_mpeg, ".mpeg": probe_mpeg, ".vob": probe_mpeg, ".ts": probe_mpeg, ".m2ts": probe_mpeg, ".mts": probe_mpeg}

def probe_builtin(path):
    ext = os.path.splitext(path)[1].lower()
    order = [READERS[ext]] if ext in READERS else []
    order += [r for r in (probe_mp4, probe_mkv, probe_avi) if r not in order]
    try:
        fsize = os.path.getsize(path)
        with open(path, "rb") as f:
            for r in order:
                try:
                    wh = r(f, fsize)
                    if wh and wh[0] > 0 and wh[1] > 0: return int(wh[0]), int(wh[1])
                except Exception: continue
    except OSError: pass
    return None

def _drive_root(path):
    m = re.match(r"^([A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+[\\/]?)", path or "")
    return m.group(1) if m else ("/" if (path or "").startswith("/") else None)

def is_mounted(path):
    r = _drive_root(path)
    return bool(r) and os.path.exists(r)

def detect(paths):
    """paths: list of file paths for one film.
    Returns dict(resolution, width, height, method) or None when every file is on an unmounted drive
    (caller should retry later). resolution may be None when nothing could be determined."""
    reachable, any_mounted, best = [], False, None
    for p in paths:
        if not is_mounted(p): continue
        any_mounted = True
        if os.path.isfile(p): reachable.append(p)
    if not any_mounted and paths: return None
    for p in reachable:
        wh, how = probe_ffprobe(p), "ffprobe"
        if not wh: wh, how = probe_builtin(p), "header"
        if wh:
            r = dict(resolution=bucket(*wh), width=wh[0], height=wh[1], method=how)
            if best is None or RANK.get(r["resolution"], -1) > RANK.get(best["resolution"], -1): best = r
    if best: return best
    for p in paths:
        g = guess_from_name(p)
        if g: return dict(resolution=g, width=None, height=None, method="filename")
    return dict(resolution=None, width=None, height=None, method="none")

def method_name():
    return "ffprobe" if ffprobe_path() else "built-in MP4/MKV/AVI/MPEG header reader + filename tokens"
