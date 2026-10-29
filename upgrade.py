"""Upgrade file: swap one of an item's video files for a better copy (e.g. a 1080p release replacing a 720p one).

The item keeps its id, tags, cast, poster, hand edits and watch data: only its files row is repointed to the new file
(size, fingerprint, missing flag) and the file-derived fields are refreshed (resolution and its pixel size, runtime when
it came from the old file; hand-edited values are kept). The old file goes to the Recycle Bin, never a permanent delete;
when that is not possible it stays where it is and is put on the Removed items list so scans do not add it back.
Exclusion records that would match the new file are forgotten. Subtitle files named after the old file are only listed
here; rename_sidecars() renames them once the user confirms."""
import os, time, shutil, json, threading
import db
import resolution as resmod
import subtitles as submod

LOCK = threading.Lock()  # one upgrade at a time

def _n(p): return os.path.normcase(os.path.abspath(p))

def lib_roots(c):
    rs = list(db.get_setting(c, "roots", []) or []) + list(db.get_setting(c, "scene_roots", []) or [])
    return [r for r in dict.fromkeys(rs) if r and str(r).strip()]

def inside_roots(p, roots):
    """True when p lies strictly inside one of the library folders."""
    np_ = os.path.normcase(os.path.realpath(os.path.abspath(p)))
    for r in roots:
        nr = os.path.normcase(os.path.realpath(os.path.abspath(r)))
        try:
            if np_ != nr and os.path.commonpath([np_, nr]) == nr: return True
        except ValueError: continue  # other drive
    return False

def clean_path(p):
    p = (p or "").strip().strip('"').strip("'").strip()
    return os.path.normpath(p) if p else ""

def probe(path):
    """{path, exists, size, width, height, resolution, duration (seconds), method} for one video file (read-only)."""
    out = dict(path=path, exists=bool(path) and os.path.isfile(path), size=None, width=None, height=None,
               resolution=None, duration=None, method=None)
    if not out["exists"]: return out
    try: out["size"] = os.path.getsize(path)
    except OSError: pass
    wh, how = resmod.probe_ffprobe(path), "ffprobe"
    if not wh: wh, how = resmod.probe_builtin(path), "header"
    if wh: out.update(width=wh[0], height=wh[1], resolution=resmod.bucket(*wh), method=how)
    else:
        g = resmod.guess_from_name(path)
        if g: out.update(resolution=g, method="filename")
    d = resmod.probe_duration(path)
    out["duration"] = round(d, 2) if d else None
    return out

def sidecars(old_video, new_video):
    """Subtitle files beside the old video whose name starts with the old file name (e.g. 'Film 720p.en.srt'), each with
    the name it would get next to the new video. [{path, target, ok, reason}]."""
    d = os.path.dirname(old_video); st = os.path.splitext(os.path.basename(old_video))[0]
    nd = os.path.dirname(new_video); nst = os.path.splitext(os.path.basename(new_video))[0]
    try: names = sorted(os.listdir(d))
    except OSError: return []
    out = []
    for x in names:
        p = os.path.join(d, x)
        if os.path.splitext(x)[1].lower() not in submod.SUB_EXT or not os.path.isfile(p): continue
        if not x.lower().startswith(st.lower() + "."): continue
        t = os.path.join(nd, nst + x[len(st):])
        it = dict(path=p, target=t, ok=True, reason=None)
        if _n(t) == _n(p): it.update(ok=False, reason="Already matches the new file")
        elif os.path.exists(t): it.update(ok=False, reason="A file with the new name is already there")
        elif not db.same_drive(p, t): it.update(ok=False, reason="On another drive")
        out.append(it)
    return out

def blockers(c, path, size, h=None):
    """Exclusion (Removed items) rows that match this file: same path, same name + size, or same size + fingerprint."""
    ids = [r[0] for r in c.execute("SELECT id FROM excluded WHERE path=? COLLATE NOCASE", (path,))]
    if size:
        name = os.path.basename(path).lower()
        for r in c.execute("SELECT id, path, oshash FROM excluded WHERE size=?", (size,)).fetchall():
            if os.path.basename((r[1] or "").replace("\\", "/")).lower() == name: ids.append(r[0]); continue
            if r[2]:
                h = h or db.file_hash(path)
                if h and r[2] == h: ids.append(r[0])
    return list(dict.fromkeys(ids))

# ---------- Recycle Bin (never a permanent delete) ----------
def recycle_problem(path):
    """None when the file can go to the Recycle Bin, else why not (network/removable drives have none)."""
    if os.name != "nt":
        try: import send2trash  # noqa: F401
        except ImportError: return "The Recycle Bin is only available on Windows here"
        return None
    if path.startswith("\\\\"): return "Network locations have no Recycle Bin"
    import ctypes
    root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
    t = ctypes.windll.kernel32.GetDriveTypeW(root)
    if t == 2: return "Removable drives have no Recycle Bin"
    if t == 4: return "Network drives have no Recycle Bin"
    if t not in (3,): return "This drive has no Recycle Bin"
    try: size = os.path.getsize(path)
    except OSError: size = None
    return _bin_off(_volume_guid(root), size)

# Windows silently deletes for good (even when asked to recycle) when the Recycle Bin is switched off for the drive
# ('Don't move files to the Recycle Bin'), by policy, or when the file is bigger than the drive's bin. Checked first.
_BB = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
_POL = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"

def _reg(hive, key, name):
    """A registry value (hive 'HKCU' or 'HKLM') or None."""
    try:
        import winreg
        with winreg.OpenKey(getattr(winreg, "HKEY_CURRENT_USER" if hive == "HKCU" else "HKEY_LOCAL_MACHINE"), key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except (OSError, ImportError): return None

def _volume_guid(root):
    try:
        import ctypes, re
        buf = ctypes.create_unicode_buffer(100)
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 100):
            m = re.search(r"\{[0-9A-Fa-f-]+\}", buf.value)
            return m.group(0) if m else None
    except Exception: pass
    return None

def _bin_off(guid, size=None):
    """Why this volume's Recycle Bin would not keep the file, or None."""
    if any(_reg(h, _POL, "NoRecycleFiles") == 1 for h in ("HKLM", "HKCU")):
        return "Windows is set by policy to delete files right away instead of using the Recycle Bin"
    vol = _BB + "\\Volume\\" + guid if guid else None
    nuke = _reg("HKCU", vol, "NukeOnDelete") if vol else None
    if nuke is None: nuke = _reg("HKCU", _BB, "NukeOnDelete")
    if nuke == 1: return "the Recycle Bin is turned off for this drive (Windows would delete the file right away)"
    cap = _reg("HKCU", vol, "MaxCapacity") if vol else None
    if cap and size and size > int(cap) * 1024 * 1024: return "the file is bigger than this drive's Recycle Bin"
    return None

def recycle(path):
    """Send one file to the Recycle Bin. Returns None when done, else the error text (the file is then left alone)."""
    why = recycle_problem(path)
    if why: return why
    if os.name != "nt":
        try:
            import send2trash; send2trash.send2trash(path)
        except Exception as e: return f"Could not move it to the trash ({e})"
        return None if not os.path.exists(path) else "Still there afterwards"
    import ctypes
    from ctypes import wintypes
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x400, 0x4000
    op = SHFILEOPSTRUCTW(hwnd=None, wFunc=FO_DELETE, pFrom=os.path.abspath(path) + "\0", pTo=None,
                         fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0: return f"Windows could not move it to the Recycle Bin (code {rc})"
    if op.fAnyOperationsAborted: return "Moving it to the Recycle Bin was cancelled"
    return None if not os.path.exists(path) else "Still there afterwards"

# ---------- plan (also the dry run) ----------
def plan(c, fid, file_id, new_path):
    """What an upgrade would do, without changing anything. Returns (plan dict, None) or (None, (error, http status))."""
    f = c.execute("SELECT id, title, kind FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return None, ("not found", 404)
    files = c.execute("SELECT id, path, size FROM files WHERE film_id=? ORDER BY id", (fid,)).fetchall()
    if not files: return None, ("This item has no file on record to replace", 400)
    if file_id in (None, "") and len(files) == 1: file_id = files[0]["id"]
    try: file_id = int(file_id)
    except (TypeError, ValueError): return None, ("Choose which file to replace", 400)
    old = next((x for x in files if x["id"] == file_id), None)
    if not old: return None, ("That file does not belong to this item", 400)
    new_path = clean_path(new_path)
    if not new_path: return None, ("Choose the new video file", 400)
    if not os.path.isabs(new_path): return None, ("Use the full path of the new file (e.g. D:\\Movies\\Film (1999).mkv)", 400)
    if not os.path.isfile(new_path): return None, ("The new file was not found: " + new_path, 400)
    if os.path.splitext(new_path)[1].lower() not in db.VIDEO_EXT: return None, ("That is not a video file", 400)
    if _n(new_path) == _n(old["path"]): return None, ("That is the file the item already uses", 400)
    other = c.execute("SELECT x.film_id, f.title FROM files x JOIN films f ON f.id=x.film_id WHERE x.path=? COLLATE NOCASE", (new_path,)).fetchone()
    if other:
        if other[0] == fid: return None, ("That file is already one of this item's files", 400)
        return None, (f"That file is already in the library as \"{other[1]}\" (item {other[0]})", 400)
    roots = lib_roots(c); inside = inside_roots(new_path, roots)
    old_dir = os.path.dirname(old["path"]); target = os.path.join(old_dir, os.path.basename(new_path))
    move_ok, move_note = False, None
    if not inside:
        if not os.path.isdir(old_dir): move_note = "The old file's folder is not reachable"
        elif os.path.exists(target) and _n(target) != _n(old["path"]): move_note = "A file with that name is already in the old file's folder"
        else:
            move_ok = True
            if not db.same_drive(new_path, target): move_note = "Another drive: the file is copied, then removed from its current folder"
    o, n = probe(old["path"]), probe(new_path)
    rp = recycle_problem(old["path"]) if o["exists"] else None
    return dict(film=dict(id=f["id"], title=f["title"], kind=f["kind"]), file_id=file_id,
                files=[dict(id=x["id"], path=x["path"]) for x in files], old=o, new=n,
                inside_library=inside, move_target=None if inside else target, move_possible=move_ok, move_note=move_note,
                same_name_as_old=_n(target) == _n(old["path"]),
                recycle=dict(possible=o["exists"] and not rp, reason=rp if o["exists"] else "The old file is already gone"),
                sidecars=sidecars(old["path"], target if (not inside and move_ok) else new_path),
                blockers=len(blockers(c, new_path, n["size"])), ffprobe=bool(resmod.ffprobe_path()),
                summary=summary(o, n)), None

def _label(p):
    return p.get("resolution") or (f"{p['width']}×{p['height']}" if p.get("width") else "unknown")

def summary(o, n):
    return f"Upgraded {_label(o)} → {_label(n)}"

def _move(src, dst):
    if os.path.exists(dst): raise OSError(f"{dst} already exists")
    if db.same_drive(src, dst): os.rename(src, dst)
    else:
        shutil.copy2(src, dst)
        if os.path.getsize(dst) != os.path.getsize(src): os.remove(dst); raise OSError("The copy is incomplete")
        os.remove(src)

def apply(c, fid, file_id, new_path, move=False, recycler=None):
    """Do the upgrade. Returns (result dict, None) or (None, (error, http status)). Order keeps every stop safe:
    move the new file (if asked) -> repoint the files row and refresh fields (one commit) -> old file to the Recycle Bin."""
    recycler = recycler or recycle
    with LOCK:
        p, err = plan(c, fid, file_id, new_path)
        if err: return None, err
        file_id = p["file_id"]; src = p["new"]["path"]; old = p["old"]["path"]; final = src; notes = []
        if move and p["inside_library"]: move = False
        if move and not p["move_possible"]: return None, (p["move_note"] or "The file can't be moved there", 400)
        recycled, recycle_err = False, None
        if move and p["same_name_as_old"]:  # same name: the old file has to leave first
            if not p["recycle"]["possible"] and p["old"]["exists"]:
                return None, ("The new file has the same name as the old one, and the old one can't go to the Recycle Bin ("
                              + (p["recycle"]["reason"] or "?") + "). Rename the new file or don't move it", 400)
            if p["old"]["exists"]:
                e = recycler(old)
                if e: return None, ("The old file could not be moved to the Recycle Bin: " + e, 500)
                recycled = True
        if move:
            try: _move(src, p["move_target"]); final = p["move_target"]
            except OSError as e:
                if not recycled: return None, (f"Could not move the new file: {e}", 500)
                notes.append(f"Could not move the new file ({e}); the item uses it where it is")
        size = os.path.getsize(final); h = db.file_hash(final)
        f = c.execute("SELECT runtime, edited_fields, storage FROM films WHERE id=?", (fid,)).fetchone()
        ed = set(json.loads(f["edited_fields"] or "[]"))
        try:
            c.execute("UPDATE files SET path=?, size=?, oshash=?, missing=0 WHERE id=? AND film_id=?", (final, size, h, file_id, fid))
            c.execute("DELETE FROM sub_probe WHERE path=? COLLATE NOCASE", (old,))
            gone = blockers(c, final, size, h) + (blockers(c, src, size, h) if final != src else [])
            for x in dict.fromkeys(gone): c.execute("DELETE FROM excluded WHERE id=?", (x,))
            paths = [r[0] for r in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (fid,))]
            r = resmod.detect(paths) or {}
            res_kept = None
            if r.get("width"): c.execute("UPDATE films SET res_width=?, res_height=? WHERE id=?", (r["width"], r["height"], fid))
            if r.get("resolution"):
                if "resolution" in ed: res_kept = r["resolution"]
                else: c.execute("UPDATE films SET resolution=? WHERE id=?", (r["resolution"], fid))
            c.execute("UPDATE films SET res_checked=1, missing=0 WHERE id=?", (fid,))
            runtime_set = None
            od = p["old"]["duration"]; nrt = resmod.runtime_minutes(paths)
            if nrt and "runtime" not in ed and (not f["runtime"] or (od and abs(f["runtime"] - max(1, round(od / 60))) <= 1)):
                if nrt != f["runtime"]: c.execute("UPDATE films SET runtime=? WHERE id=?", (nrt, fid)); runtime_set = nrt
            od_, nd_ = os.path.splitdrive(old)[0], os.path.splitdrive(final)[0]
            if od_ and nd_ and os.path.normcase(od_) != os.path.normcase(nd_) and "storage" not in ed and (f["storage"] or "") == od_:
                c.execute("UPDATE films SET storage=? WHERE id=?", (nd_, fid))
            c.commit()
        except Exception:
            c.rollback(); raise
        if p["old"]["exists"] and not recycled:
            recycle_err = recycler(old)
            recycled = not recycle_err
            if recycle_err and os.path.exists(old):  # left in place: keep scans from adding it back as a new item
                t = c.execute("SELECT title, kind, studio, cast_, release_date FROM films WHERE id=?", (fid,)).fetchone()
                c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at,studio,cast_,release_date) VALUES(?,?,?,?,?,?,?,?,?)",
                          (old, os.path.getsize(old), db.file_hash(old), (t["title"] or "") + " (replaced by upgrade)", t["kind"], time.time(),
                           t["studio"], t["cast_"], t["release_date"]))
                c.commit()
        n = dict(p["new"], path=final)
        gal = None
        if p["film"]["kind"] == "scene":
            import gallery as gallerymod
            k, g = gallerymod.find(old, db.VIDEO_EXT)
            if g and k == "canonical": gal = g  # named after the old file: left as is (galleries are never touched here)
        return dict(ok=True, film_id=fid, file_id=file_id, old=p["old"], new=n, moved=final != src, path=final,
                    recycled=recycled, recycle_error=recycle_err, blockers_removed=len(dict.fromkeys(gone)),
                    resolution_kept_hand_edit=res_kept, runtime=runtime_set, sidecars=sidecars(old, final),
                    gallery_left=gal, notes=notes, summary=summary(p["old"], n)), None

def rename_sidecars(c, fid, file_id, old_path, paths):
    """Rename the confirmed subtitle files (named after the replaced file) to match the item's current file.
    Only files that sidecars() itself lists as renamable are touched; never overwrites. -> (renamed, errors)."""
    r = c.execute("SELECT path FROM files WHERE id=? AND film_id=?", (file_id, fid)).fetchone()
    if not r: return None, ["That file does not belong to this item"]
    old_path = clean_path(old_path)
    if not old_path or c.execute("SELECT 1 FROM files WHERE path=? COLLATE NOCASE", (old_path,)).fetchone():
        return None, ["That is not a replaced file"]
    if os.path.dirname(_n(old_path)) != os.path.dirname(_n(r[0])) and not inside_roots(old_path, lib_roots(c)):
        return None, ["That is not a replaced file"]
    want = {_n(p) for p in (paths or [])}
    done, errors = [], []
    for it in sidecars(old_path, r[0]):
        if _n(it["path"]) not in want: continue
        if not it["ok"]: errors.append(f"{it['path']}: {it['reason']}"); continue
        try: os.rename(it["path"], it["target"]); done.append(dict(path=it["path"], target=it["target"]))
        except OSError as e: errors.append(f"{it['path']}: {e.strerror or e}")
    return done, errors
