"""Backup copies of the user's CURRENT chosen artwork next to the video files (Film Tagger stays authoritative).

Only called after an explicit user action (poster upload/URL/gallery/Find Posters/restore/editor, person photo
upload/crop/reset) or the explicit Export buttons/endpoints. Never batch, never deletes, never overwrites a file it did
not write itself, never reads folder art back into the app.

Film poster naming, per existing parent folder of the film's (non-missing) video files:
  * a poster.* / poster-filmtagger.* already holding the same bytes -> nothing written (idempotent);
  * no poster.* in the folder -> poster.<ext>;
  * otherwise -> poster-filmtagger.<ext> (the user's own Poster.jpg etc. is left alone).
  A file is only replaced when its current bytes equal what Film Tagger last wrote there (recorded in settings); the old
  copy is first moved to "Film Tagger Artwork/backup/". Any other differing file is never touched.
Person photo: one deterministic folder - the video folder of the lowest-id film crediting the person (cast or
director) that has an existing folder -> <folder>/Film Tagger Artwork/People/<Name>.<ext>, same replace rules."""
import hashlib, json, os, re, shutil, time
import db

SUB = "Film Tagger Artwork"
EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

def _key(path): return "artexport:" + os.path.normcase(os.path.abspath(path))

def film_folders(c, fid):
    out = []
    for (p,) in c.execute("SELECT path FROM files WHERE film_id=? AND COALESCE(missing,0)=0 ORDER BY id", (fid,)):
        d = os.path.dirname(p or "")
        if d and os.path.isdir(d) and os.path.normcase(d) not in [os.path.normcase(x) for x in out]: out.append(d)
    return out

def _write(c, src, dst, digest):
    """Copy src to dst unless that would clobber a foreign file. -> status string."""
    if os.path.exists(dst):
        cur = sha(dst)
        if cur == digest: return "same"
        if (db.get_setting(c, _key(dst)) or {}).get("sha") != cur: return "foreign"  # not ours: never touch
        bdir = os.path.join(_artroot(dst), "backup")
        os.makedirs(bdir, exist_ok=True)
        stem, ext = os.path.splitext(os.path.basename(dst)); b = os.path.join(bdir, f"{stem}-{int(time.time())}{ext}"); n = 0
        while os.path.exists(b): n += 1; b = os.path.join(bdir, f"{stem}-{int(time.time())}-{n}{ext}")
        shutil.move(dst, b)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".ft-tmp"
    shutil.copyfile(src, tmp); os.replace(tmp, dst)
    db.set_setting(c, _key(dst), {"sha": digest, "at": time.time()}); c.commit()
    return "written"

def _artroot(dst):
    d = os.path.dirname(dst)
    if os.path.basename(d) == "People" and os.path.basename(os.path.dirname(d)) == SUB: return os.path.dirname(d)
    return os.path.join(d, SUB)

def _folder_posters(d):
    try: names = os.listdir(d)
    except OSError: return []
    return [n for n in names if os.path.splitext(n)[1].lower() in EXTS and os.path.splitext(n)[0].lower() in ("poster", "poster-filmtagger")]

def export_poster(c, fid, src):
    """src: local file of the film's current custom poster. -> list of {folder, file, status}."""
    if not src or not os.path.isfile(src): return [dict(status="no local poster")]
    digest = sha(src); ext = os.path.splitext(src)[1].lower() or ".jpg"
    res = []
    for d in film_folders(c, fid):
        try:
            existing = _folder_posters(d); hit = next((n for n in existing if sha(os.path.join(d, n)) == digest), None)
            if hit: res.append(dict(folder=d, file=hit, status="same")); continue
            plain = [n for n in existing if os.path.splitext(n)[0].lower() == "poster"]
            name = "poster" + ext
            if plain:  # poster.* exists: reuse it only if we wrote it, else use the Film Tagger name
                ours = next((n for n in plain if (db.get_setting(c, _key(os.path.join(d, n))) or {}).get("sha") == sha(os.path.join(d, n))), None)
                name = ours if ours and os.path.splitext(ours)[1].lower() == ext else "poster-filmtagger" + ext
            st = _write(c, src, os.path.join(d, name), digest)
            if st == "foreign": name = "poster-filmtagger" + ext; st = _write(c, src, os.path.join(d, name), digest)
            res.append(dict(folder=d, file=name, status=st))
        except OSError as e:
            res.append(dict(folder=d, status=f"error: {type(e).__name__}"))
    return res or [dict(status="no video folder")]

def slug(name):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", " ".join((name or "").split())).strip(" .")
    return s[:120] or "person"

def person_folder(c, name):
    n = " ".join((name or "").split())
    if not n: return None
    for (fid,) in c.execute("SELECT id FROM films WHERE person_in_any(cast_, ?) OR person_in_any(director, ?) ORDER BY id", (n, n)):
        f = film_folders(c, fid)
        if f: return f[0]
    return None

def export_person(c, name, src):
    if not src or not os.path.isfile(src): return dict(status="no local photo")
    d = person_folder(c, name)
    if not d: return dict(status="no video folder")
    ext = os.path.splitext(src)[1].lower() or ".jpg"
    dst = os.path.join(d, SUB, "People", slug(name) + ext)
    try: st = _write(c, src, dst, sha(src))
    except OSError as e: st = f"error: {type(e).__name__}"
    if st == "foreign":  # a file we did not write: keep it, use a suffixed name
        dst = os.path.join(d, SUB, "People", slug(name) + "-filmtagger" + ext); st = _write(c, src, dst, sha(src))
    return dict(folder=d, file=dst, status=st)
