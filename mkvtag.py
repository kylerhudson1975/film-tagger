"""MKV container titles (optional; needs MKVToolNix's mkvpropedit).

Hard-matched scenes: after ThePornDB match + rename, writes segment title
  '<Studio> - <Title> (YYYY-MM-DD)'
and global tags TITLE, DATE_RELEASED, ACTOR, ARTIST, PRODUCTION_STUDIO.

Regular (non-adult) movies: writes segment title 'Title (Year)' (and TITLE / DATE_RELEASED tags) for .mkv files.
Skipped when the container title already matches. Never renames or moves files.

Header edit only (no re-encode). Values come from the library entry. Never fatal: failures go to mkv_titles.log next
to the database. Files in use are skipped. File mtime is kept; size/fingerprint refreshed afterwards.
MKVToolNix is found on PATH or in its default install folder; without it the feature is simply off."""
import os, sys, json, time, shutil, hashlib, logging, tempfile, subprocess
from xml.sax.saxutils import escape

log = logging.getLogger("filmtagger.mkvtag")
_TOOL = {}  # name -> path or None (cached; reset() clears)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def reset(): _TOOL.clear()

def find_tool(name="mkvpropedit"):
    """Path of an MKVToolNix program (PATH first, then the default install folders), or None."""
    if name in _TOOL: return _TOOL[name]
    exe = name + (".exe" if os.name == "nt" else "")
    p = shutil.which(name)
    if not p:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)"),
                     r"C:\Program Files", r"C:\Program Files (x86)"):
            if base and os.path.isfile(os.path.join(base, "MKVToolNix", exe)): p = os.path.join(base, "MKVToolNix", exe); break
    _TOOL[name] = p
    return p

def available(): return bool(find_tool("mkvpropedit"))

def performers(cast):
    return [x.strip() for x in (cast or "").split(",") if x.strip()]

def scene_title(f):
    """'Studio - Title (YYYY-MM-DD)'; parts that are empty are left out."""
    title = (f["title"] or "").strip(); studio = (f["studio"] or "").strip(); date = (f["release_date"] or "").strip()
    s = f"{studio} - {title}" if studio and title else (title or studio)
    return f"{s} ({date})" if date and s else s


def movie_title(f):
    """'Title (Year)'; year left out when missing."""
    t = (f["title"] or "").strip(); y = f["year"] if "year" in f.keys() else None
    return f"{t} ({y})" if t and y not in (None, "") else t

def desired_movie(f):
    """-> dict(title, tags=...) for a regular movie row (title, year)."""
    t = movie_title(f); tags = [("TITLE", (f["title"] or "").strip())]
    y = f["year"] if "year" in f.keys() else None
    if y not in (None, ""): tags.append(("DATE_RELEASED", str(y)))
    return dict(title=t, tags=[x for x in tags if x[1]])

def desired(f):
    """-> dict(title, tags=[(name, value), ...]) for a scene row (title, studio, release_date, cast_)."""
    t = scene_title(f); perf = performers(f["cast_"])
    tags = [("TITLE", (f["title"] or "").strip())]
    if (f["release_date"] or "").strip(): tags.append(("DATE_RELEASED", f["release_date"].strip()))
    tags += [("ACTOR", p) for p in perf]
    if perf: tags.append(("ARTIST", ", ".join(perf)))
    if (f["studio"] or "").strip(): tags.append(("PRODUCTION_STUDIO", f["studio"].strip()))
    return dict(title=t, tags=[x for x in tags if x[1]])

def signature(d): return hashlib.sha1(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

def tags_xml(tags):
    """Matroska tags XML with one global tag block (TargetTypeValue 50 = the movie/scene itself)."""
    simple = "".join(f"<Simple><Name>{escape(n)}</Name><String>{escape(v)}</String></Simple>" for n, v in tags)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE Tags SYSTEM "matroskatags.dtd">\n'
            f"<Tags><Tag><Targets><TargetTypeValue>50</TargetTypeValue></Targets>{simple}</Tag></Tags>\n")

def in_use(path):
    """True if another program has the file open (Windows: exclusive open fails with a sharing violation)."""
    if os.name != "nt": return False
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k.CreateFileW(path, 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)  # GENERIC_READ|WRITE, no sharing, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value, -1):
        return ctypes.get_last_error() in (32, 33)  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
    k.CloseHandle(h); return False

def _log_file():
    import db
    return os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "mkv_titles.log")

def note(msg):
    """Log one line to mkv_titles.log (and the logger). Never raises."""
    log.info(msg)
    try:
        with open(_log_file(), "a", encoding="utf-8", newline="\n") as fh: fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError: pass

def ensure_table(c):
    """mkv_meta: what Film Tagger last wrote into each MKV (so passes skip unchanged files and spot titles edited by hand)."""
    c.execute("CREATE TABLE IF NOT EXISTS mkv_meta(file_id INTEGER PRIMARY KEY, path TEXT, sig TEXT, size INTEGER, written_at REAL, title TEXT)")

def write(path, d, run=None, timeout=120):
    """Write title + global tags into one .mkv in place. -> (ok, message). Never raises."""
    run = run or subprocess.run
    tool = find_tool("mkvpropedit")
    if not tool: return False, "MKVToolNix (mkvpropedit) is not installed"
    if os.path.splitext(path)[1].lower() != ".mkv": return False, "not an .mkv file"
    if not os.path.isfile(path): return False, "file not found"
    try:
        if in_use(path): return False, "file is in use by another program"
    except Exception: pass
    try: st = os.stat(path)
    except OSError as e: return False, f"cannot read file: {e}"
    fd, xml = tempfile.mkstemp(suffix=".xml", prefix="ft_mkvtags_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh: fh.write(tags_xml(d["tags"]))
        cmd = [tool, path, "--edit", "info", "--set", "title=" + d["title"], "--tags", "global:" + xml]
        try:
            r = run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW) if os.name == "nt" else run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired: return False, "mkvpropedit timed out"
        except OSError as e: return False, f"could not run mkvpropedit: {e}"
        if r.returncode not in (0, 1):  # 1 = warnings only
            out = (r.stdout or b"") + (r.stderr or b"")
            out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
            msg = " ".join(l.strip() for l in out.splitlines() if "rror" in l)[:300] or f"exit code {r.returncode}"
            return False, "mkvpropedit failed: " + msg
        try: os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # keep the file's dates (media servers, 'recently added')
        except OSError: pass
        return True, "ok"
    finally:
        try: os.remove(xml)
        except OSError: pass

def read_title(path, run=None):
    """Current segment title via mkvmerge -J, or None if mkvmerge is missing/fails."""
    run = run or subprocess.run; tool = find_tool("mkvmerge")
    if not tool: return None
    try:
        kw = dict(capture_output=True, timeout=60)
        if os.name == "nt": kw["creationflags"] = NO_WINDOW
        r = run([tool, "-J", path], **kw)
        return (json.loads(r.stdout or b"{}").get("container") or {}).get("properties", {}).get("title")
    except Exception: return None
