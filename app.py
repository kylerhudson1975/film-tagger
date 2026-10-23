"""Film Tagger - local film library cataloging & tagging. Run: python app.py -> http://localhost:5050"""
import os, io, csv, json, time, urllib.request, urllib.parse, urllib.error
from flask import Flask, request, jsonify, send_from_directory, send_file, Response
import db
import resolution as resmod
import subtitles as submod
import tpdb
import focus as focusmod
import gallery as gallerymod

APP_VERSION = "1.2.0"  # keep in sync with installer/FilmTagger.nsi APPVER
REPO = "kylerhudson1975/film-tagger"

app = Flask(__name__, static_folder="static")
FILM_FIELDS = ["title", "year", "mpaa", "director", "cast_", "runtime", "plot", "poster", "rating", "watched", "notes", "storage", "tmdb_id", "original_title", "user_akas",
               "writers", "producers", "cinematographer", "composer", "country", "studio", "resolution", "primary_genre", "aznude_status", "subtitles", "release_date", "tpdb_id"]

LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}

@app.before_request
def local_only_guard():
    """Block other websites (CSRF) and DNS-rebinding from driving this local app."""
    host = (request.host or "").rsplit(":", 1)[0].lower()
    if host not in LOCAL_HOSTS: return jsonify(error="Forbidden host"), 403
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        oh = urllib.parse.urlparse(origin).hostname or ""
        if oh.lower() not in {"localhost", "127.0.0.1", "::1"}: return jsonify(error="Forbidden origin"), 403

def C(): return db.connect()

def _alias_map():
    c = db.connect()
    try: return db.alias_nrm_map(c)
    finally: c.close()
tpdb.ALIAS_SOURCE = _alias_map  # ThePornDB matching counts a performer alias as a performer match
def ok(**k): return jsonify(dict(ok=True, **k))

@app.route("/")
def index(): return send_from_directory(app.static_folder, "index.html")

# ---------- films ----------
def split_list(v):
    if not v: return []
    if isinstance(v, list): return [x.strip() for x in v if x and x.strip()]
    return [x.strip() for x in v.split(",") if x.strip()]

def build_query(q):
    where, args = [], []
    T = "SELECT ft.film_id FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved' AND tag_norm(t.name)=tag_norm(?)"
    for name in split_list(q.get("all")):          # AND
        where.append(f"f.id IN ({T})"); args.append(name)
    anyl = split_list(q.get("any"))                 # OR
    if anyl:
        where.append(f"f.id IN (SELECT ft.film_id FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved' AND tag_norm(t.name) IN ({','.join('tag_norm(?)' for _ in anyl)}))"); args += anyl
    for name in split_list(q.get("none")):         # NOT
        where.append(f"f.id NOT IN ({T})"); args.append(name)
    text = (q.get("q") or "").strip()
    if text:
        field = q.get("in") or "all"
        TAGQ = "f.id IN (SELECT ft.film_id FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved' AND (t.name LIKE ? OR tag_norm(t.name) LIKE '%' || tag_norm(?) || '%'))"
        PATHQ = "f.id IN (SELECT film_id FROM files WHERE path LIKE ?)"
        TITLEQ = "title_match(f.title, f.original_title, COALESCE(f.alt_titles,'') || ' ; ' || COALESCE(f.user_akas,''), ?)"
        single = {"title": TITLEQ, "plot": "f.plot LIKE ?", "cast": "f.cast_ LIKE ?", "director": "f.director LIKE ?",
                  "notes": "f.notes LIKE ?", "tags": TAGQ, "path": PATHQ, "country": "f.country LIKE ?", "studio": "fold_in(f.studio, ?)", "resolution": "f.resolution LIKE ?", "subtitles": "f.subtitles LIKE ?"}
        tw, ta = [], []
        for w in text.split():
            like = f"%{w}%"
            if field in single:
                tw.append(single[field]); ta.append(w if field in ("title", "studio") else like)
                if field == "tags": ta.append(w)
            else:
                tw.append(f"({TITLEQ} OR f.director LIKE ? OR f.cast_ LIKE ? OR f.plot LIKE ? OR f.notes LIKE ? OR f.country LIKE ? OR fold_in(f.studio, ?) OR f.resolution LIKE ? OR f.subtitles LIKE ? OR CAST(f.year AS TEXT)=? OR {PATHQ} OR {TAGQ})")
                ta += [w, like, like, like, like, like, w, like, like, w, like, like, w]
        als = db.search_alias_names(C(), text) if field in ("cast", "people", "all") else []
        sts = [n for m in db.studio_alias_search(text) for n in db.studio_names(m)] if field in ("studio", "cast", "all") else []
        ors, oa = [], []
        if als: ors.append("person_in_any(f.cast_, ?)"); oa.append(db.ALIAS_SEP.join(als))  # performer aliases: 'Erica Black' also finds 'Daphne Klyde'
        if sts: ors.append("person_in_any(f.studio, ?)"); oa.append(db.ALIAS_SEP.join(sts))  # studio aliases: 'angels.love' finds 'Angels Love'
        if ors:
            where.append("((" + " AND ".join(tw) + ") OR " + " OR ".join(ors) + ")"); args += ta + oa
        else:
            where += tw; args += ta
    for key, col, op in [("year_min", "year", ">="), ("year_max", "year", "<="), ("rating_min", "rating", ">="),
                         ("rating_max", "rating", "<="), ("runtime_min", "runtime", ">="), ("runtime_max", "runtime", "<=")]:
        v = q.get(key)
        if v not in (None, ""): where.append(f"f.{col} {op} ?"); args.append(int(v))
    w = q.get("watched")
    if w in ("1", "0", 1, 0): where.append("COALESCE(f.watched,0)=?"); args.append(int(w))
    if q.get("missing") in ("1", 1): where.append("f.missing=1")
    if q.get("pending") in ("1", 1): where.append("f.id IN (SELECT film_id FROM film_tags WHERE status='pending')")
    if q.get("queued") in ("1", 1): where.append("f.id IN (SELECT film_id FROM smart_queue)")
    if q.get("aznude_queued") in ("1", 1): where.append("f.id IN (SELECT film_id FROM aznude_queue)")
    azs = split_list(q.get("aznude"))
    if azs:  # OR across AZNude statuses; "(none)"/"Unchecked" = not checked yet
        vals = []
        for x in azs:
            try: vals.append(db.norm_aznude_status("" if x == "(none)" else x))
            except ValueError: pass
        named = [v for v in vals if v]
        parts = [f"f.aznude_status IN ({','.join('?' * len(named))})"] if named else []
        if None in vals: parts.append("f.aznude_status IS NULL")
        if not parts: parts = ["0"]
        where.append("(" + " OR ".join(parts) + ")"); args += named
    mp = split_list(q.get("mpaa"))
    if mp:
        parts = ["UPPER(COALESCE(f.mpaa,'')) IN (" + ",".join("?" * len(mp)) + ")"]
        args_mp = [("" if m == "(none)" else m.upper()) for m in mp]
        where.append("(" + parts[0] + ")"); args += args_mp
    lib = (q.get("lib") or "").strip().rstrip("\\/")
    if lib:
        likes = []
        for pre in db.lib_prefixes(C(), lib):  # all scene roots count as one 'Porn Scenes' library
            esc_ = pre.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            likes += ["LOWER(path) LIKE ? ESCAPE '\\'", "LOWER(path) LIKE ? ESCAPE '\\'"]; args += [esc_ + "\\\\%", esc_ + "/%"]
        where.append("f.id IN (SELECT film_id FROM files WHERE " + " OR ".join(likes) + ")")
    if q.get("nompaa") in ("1", 1): where.append("f.tmdb_id IS NOT NULL AND COALESCE(f.mpaa,'')=''")
    if q.get("needs_tmdb") in ("1", 1): where.append("f.tmdb_status='needs_match'")
    person = (q.get("person") or "").strip()
    if person:  # exact name match against stored people lists (not a text search)
        role = q.get("prole") or ""
        cols = [db.PEOPLE_ROLES[role]] if role in db.PEOPLE_ROLES else list(db.PEOPLE_ROLES.values())
        names = db.ALIAS_SEP.join(db.expand_person(C(), person))  # the person under any of their aliases
        where.append("(" + " OR ".join(f"person_in_any(f.{col}, ?)" for col in cols) + ")"); args += [names] * len(cols)
    decs = [int(x) for x in split_list(q.get("decade")) if x.isdigit()]
    if decs:  # OR across decades
        where.append("(" + " OR ".join("(f.year>=? AND f.year<?)" for _ in decs) + ")")
        for d in decs: args += [d, d + 10]
    for cn in split_list(q.get("country")):  # exact country name (one entry of the comma-separated list)
        where.append("person_in(f.country, ?)"); args.append(cn)
    for st in split_list(q.get("studio")):  # exact studio name (one entry of the comma-separated list)
        keys = "|".join(sorted({db.studio_key(x) for x in db.studio_names(st)} - {""}))  # spelling variants + aliases
        where.append("(studio_key_in(f.studio, ?) OR f.id IN (SELECT film_id FROM files WHERE path_studio_key(path, ?) IN (SELECT value FROM json_each(?))))")
        args += [keys, "|".join(db.scene_roots(C())), json.dumps(keys.split("|"))]
    rs = split_list(q.get("res"))
    if rs:  # OR across resolutions; "(none)" = not set
        parts = ["f.resolution IN (" + ",".join("?" * len([x for x in rs if x != "(none)"])) + ")"] if [x for x in rs if x != "(none)"] else []
        if "(none)" in rs: parts.append("COALESCE(f.resolution,'')=''")
        where.append("(" + " OR ".join(parts) + ")"); args += [x for x in rs if x != "(none)"]
    sb = split_list(q.get("subs"))
    if sb:  # OR across: "(has)" any subtitles, "(none)" none detected, or a language name
        parts = []
        for x in sb:
            if x == "(has)": parts.append("COALESCE(f.subtitles,'') NOT IN ('','None')")
            elif x == "(none)": parts.append("COALESCE(f.subtitles,'') IN ('','None')")
            else: parts.append("sub_lang_in(f.subtitles, ?)"); args.append(x)
        where.append("(" + " OR ".join(parts) + ")")
    pgs = split_list(q.get("pgenre"))
    if pgs:  # OR across primary genres (accent/case-insensitive); "(none)" = not set
        parts = [f"tag_norm(f.primary_genre) IN ({','.join('tag_norm(?)' for x in pgs if x != '(none)')})"] if [x for x in pgs if x != "(none)"] else []
        if "(none)" in pgs: parts.append("COALESCE(f.primary_genre,'')=''")
        where.append("(" + " OR ".join(parts) + ")"); args += [x for x in pgs if x != "(none)"]
    if q.get("scenes") in ("1", 1): where.append("f.kind='scene'")
    if q.get("films_only") in ("1", 1): where.append("COALESCE(f.kind,'')<>'scene'")
    if q.get("untagged") in ("1", 1): where.append("f.id NOT IN (SELECT film_id FROM film_tags WHERE status='approved')")
    return ("WHERE " + " AND ".join(where)) if where else "", args

# Studio sort: within a studio, first billed performer A-Z (case/accent folded; none last), then newest release, then id.
FIRST_PERFORMER = "NULLIF(TRIM(substr(COALESCE(f.cast_,''), 1, instr(COALESCE(f.cast_,'') || ',', ',') - 1)), '')"
STUDIO_TIE = f"{FIRST_PERFORMER} IS NULL, {FIRST_PERFORMER} COLLATE FOLD, f.release_date IS NULL, f.release_date DESC, "
SORTS = {"title": "f.title COLLATE FOLD", "year": "f.year", "rating": "f.rating", "runtime": "f.runtime",
         "added": "f.added_at", "edited": "COALESCE(f.updated_at, f.added_at)", "director": "f.director COLLATE FOLD", "country": "f.country COLLATE FOLD", "studio": "f.studio COLLATE FOLD", "resolution": "CASE f.resolution WHEN 'SD' THEN 1 WHEN '480p' THEN 2 WHEN '576p' THEN 3 WHEN '720p' THEN 4 WHEN '1080p' THEN 5 WHEN '1440p' THEN 6 WHEN '4K' THEN 7 END", "watched": "f.watched", "id": "f.id", "primary_genre": "NULLIF(f.primary_genre,'') COLLATE FOLD", "aznude_status": "f.aznude_status"}

def attach(c, films, full=False):
    if not films: return films
    ids = [f["id"] for f in films]; ph = ",".join("?" * len(ids)); m = {f["id"]: f for f in films}
    for f in films: f["tags"] = []; f["pending"] = []; f["files"] = []
    for r in c.execute(f"SELECT ft.film_id, t.id, t.name, t.category, ft.status, ft.source FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id IN ({ph}) ORDER BY t.category, t.name COLLATE FOLD", ids):
        m[r[0]]["pending" if r["status"] == "pending" else "tags"].append(dict(id=r["id"], name=r["name"], category=r["category"], source=r["source"]))
    for r in c.execute(f"SELECT film_id, path, size, missing FROM files WHERE film_id IN ({ph})", ids):
        m[r[0]]["files"].append(dict(path=r["path"], size=r["size"], missing=r["missing"]))
    qs = {r[0] for r in c.execute(f"SELECT film_id FROM smart_queue WHERE film_id IN ({ph})", ids)}
    for f in films: f["queued"] = f["id"] in qs
    aq = {r[0] for r in c.execute(f"SELECT film_id FROM aznude_queue WHERE film_id IN ({ph})", ids)}
    for f in films: f["aznude_queued"] = f["id"] in aq
    return films

@app.get("/api/films")
def list_films():
    c = C(); q = request.args
    where, args = build_query(q)
    sort = SORTS.get(q.get("sort"), SORTS["title"]); order = "DESC" if q.get("order") == "desc" else "ASC"
    limit = min(int(q.get("limit", 200)), 20000); offset = int(q.get("offset", 0))
    total = c.execute(f"SELECT COUNT(*) FROM films f {where}", args).fetchone()[0]
    tie = STUDIO_TIE if q.get("sort") == "studio" else ""
    rows = [dict(r) for r in c.execute(f"SELECT f.* FROM films f {where} ORDER BY {sort} IS NULL, {sort} {order}, {tie}f.id LIMIT ? OFFSET ?", args + [limit, offset])]
    if q.get("ids_only"):
        return jsonify(ids=[r[0] for r in c.execute(f"SELECT f.id FROM films f {where}", args)], total=total)
    return jsonify(total=total, films=attach(c, rows))

@app.get("/api/decades")
def decades():
    return jsonify([dict(decade=r[0], n=r[1]) for r in C().execute(
        "SELECT (year/10)*10 d, COUNT(*) FROM films WHERE year IS NOT NULL AND year>0 GROUP BY d ORDER BY d")])

@app.get("/api/resolutions")
def resolutions():
    n = dict(C().execute("SELECT COALESCE(resolution,''), COUNT(*) FROM films GROUP BY 1").fetchall())
    return jsonify([dict(name=r, n=n.get(r, 0)) for r in resmod.CHOICES] + [dict(name="(none)", n=n.get("", 0))])

@app.get("/api/studio-aliases")
def studio_aliases_list():
    c = C()
    return jsonify(aliases=[dict(alias=r[0], canonical=r[1]) for r in c.execute("SELECT alias, canonical FROM studio_aliases ORDER BY canonical COLLATE FOLD, alias COLLATE FOLD")])

@app.post("/api/studio-aliases")
def studio_aliases_add():
    """{alias: 'angels.love', canonical: 'Angels Love'}: merge a studio spelling into a main name (updates films)."""
    d = request.json or {}; c = C()
    try: n = db.add_studio_alias(c, d.get("alias"), d.get("canonical"))
    except ValueError as e: return jsonify(error=str(e)), 400
    return jsonify(ok=True, updated=n)

@app.delete("/api/studio-aliases")
def studio_aliases_remove():
    a = (request.args.get("alias") or (request.get_json(silent=True) or {}).get("alias") or "").strip()
    if not a: return jsonify(error="alias required"), 400
    db.remove_studio_alias(C(), a); return jsonify(ok=True)

@app.get("/api/studio-links")
def studio_links_list():
    return jsonify(links=db.studio_links(C()))

@app.post("/api/studio-links")
def studio_links_add():
    """{child: 'Out of the Family', parent: "Devil's Film"}: a sub-label counts as its parent studio in ThePornDB matching."""
    d = request.json or {}
    try: db.add_studio_link(C(), d.get("child"), d.get("parent"))
    except ValueError as e: return jsonify(error=str(e)), 400
    tpdb._STUDIO_CACHE.clear(); return jsonify(ok=True)

@app.delete("/api/studio-links")
def studio_links_remove():
    ch = (request.args.get("child") or "").strip()
    if not ch: return jsonify(error="child required"), 400
    db.remove_studio_link(C(), ch); tpdb._STUDIO_CACHE.clear(); return jsonify(ok=True)

@app.get("/api/studios")
def studios():
    """Distinct studio (production company) names present in the library, with film counts."""
    n = {}
    for (v,) in C().execute("SELECT studio FROM films WHERE studio IS NOT NULL AND studio<>''"):
        for x in split_list(v): n[x] = n.get(x, 0) + 1
    return jsonify([dict(name=k, n=v) for k, v in sorted(n.items(), key=lambda kv: db.fold(kv[0]))])

@app.get("/api/primary-genres")
def primary_genres():
    """Primary genres in use (with counts) plus all Genre-category tag names, both accent-folded alphabetical."""
    c = C()
    used = [dict(name=r[0], n=r[1]) for r in c.execute("SELECT primary_genre, COUNT(*) FROM films WHERE COALESCE(primary_genre,'')<>'' GROUP BY primary_genre")]
    tags = [r[0] for r in c.execute("SELECT name FROM tags WHERE category='Genre' COLLATE NOCASE")]
    custom = db.custom_primary_genres(c)
    choices = {}
    for n in tags + custom + [u["name"] for u in used]: choices.setdefault(db.fold(n), n)
    return jsonify(used=sorted(used, key=lambda x: db.fold(x["name"])), genre_tags=sorted(tags, key=lambda x: (db.fold(x), x)),
                   custom=sorted(custom, key=lambda x: (db.fold(x), x)), choices=sorted(choices.values(), key=lambda x: (db.fold(x), x)))

@app.post("/api/primary-genre/backfill")
def primary_genre_backfill():
    """Fill empty primary genres from stored TMDB data only (never calls TMDB)."""
    return jsonify(db.backfill_primary_genre(C()))

@app.get("/api/countries")
def countries():
    """Distinct country names present in the library, with film counts."""
    n = {}
    for (v,) in C().execute("SELECT country FROM films WHERE country IS NOT NULL AND country<>''"):
        for x in split_list(v): n[x] = n.get(x, 0) + 1
    return jsonify([dict(name=k, n=v) for k, v in sorted(n.items(), key=lambda kv: db.fold(kv[0]))])

@app.get("/api/people")
def people():
    """Films featuring a person (exact name match). ?name=...&role=director|cast|writer|producer|cinematographer|composer"""
    name = (request.args.get("name") or "").strip()
    if not name: return jsonify(error="name required"), 400
    role = request.args.get("role") or ""
    if role and role not in db.PEOPLE_ROLES: return jsonify(error="unknown role"), 400
    c = C(); counts = {}
    for r, col in db.PEOPLE_ROLES.items():
        counts[r] = c.execute(f"SELECT COUNT(*) FROM films WHERE person_in_any({col}, ?)", (db.ALIAS_SEP.join(db.expand_person(c, name)),)).fetchone()[0]
    where, args = build_query({"person": name, "prole": role})
    rows = c.execute(f"SELECT f.id, f.title, f.year FROM films f {where} ORDER BY f.year IS NULL, f.year, f.title COLLATE FOLD", args).fetchall()
    return jsonify(name=name, role=role or None, total=len(rows), counts=counts,
                   films=[dict(id=r["id"], title=r["title"], year=r["year"]) for r in rows])

# ---------- custom poster art (stored in films.custom_poster; TMDB code only ever writes films.poster) ----------
POSTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "posters")
POSTER_MAX = 15 * 1024 * 1024
POSTER_SRC_MAX = 200 * 1024 * 1024   # absolute cap on a source photo we will read and shrink
POSTER_SRC_PIXELS = 250_000_000      # absolute cap on decoded pixels (about 15800 x 15800)
POSTER_FIT_PX = 2000                 # long side of a shrunk poster
POSTER_FIT_Q = 90

def fit_poster(data):
    """Bytes of a too-big image -> (jpeg bytes, 'jpg') downscaled to POSTER_FIT_PX on the long side, keeping the
    aspect ratio, or (None, error). Images already <= POSTER_MAX are not passed here (kept as-is)."""
    if len(data) > POSTER_SRC_MAX: return None, "Image is too large (max 200 MB)"
    try:
        import cv2, numpy as np
        buf = np.frombuffer(data, np.uint8)
        img = None
        for flag in (cv2.IMREAD_REDUCED_COLOR_4, cv2.IMREAD_REDUCED_COLOR_2, cv2.IMREAD_COLOR):
            img = cv2.imdecode(buf, flag)
            if img is None: return None, "That file is not a valid JPG, PNG or WebP image"
            h, w = img.shape[:2]
            if max(h, w) >= POSTER_FIT_PX or flag == cv2.IMREAD_COLOR: break
        if h * w > POSTER_SRC_PIXELS: return None, "Image has too many pixels"
        s = POSTER_FIT_PX / max(h, w)
        if s < 1: img = cv2.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv2.INTER_AREA)
        ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, POSTER_FIT_Q])
        if not ok: return None, "Could not re-encode that image"
        return out.tobytes(), "jpg"
    except Exception:
        return None, "Could not read that image"
POSTER_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

def sniff_image(b):
    if b[:3] == b"\xff\xd8\xff": return "jpg"
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "webp"
    return None

def _drop_local_poster(c, fid):
    r = c.execute("SELECT custom_poster FROM films WHERE id=?", (fid,)).fetchone()
    v = (r and r[0]) or ""
    if v.startswith("/posters/"):
        p = os.path.join(POSTERS_DIR, os.path.basename(v))
        try: os.remove(p)
        except OSError: pass

def _save_poster(c, fid, data, ext):
    os.makedirs(POSTERS_DIR, exist_ok=True)
    _drop_local_poster(c, fid)
    name = f"film-{fid}-{int(time.time())}.{ext}"  # timestamp busts the browser cache
    with open(os.path.join(POSTERS_DIR, name), "wb") as fh: fh.write(data)
    return "/posters/" + name

@app.get("/posters/<path:name>")
def poster_file(name):
    return send_from_directory(POSTERS_DIR, os.path.basename(name), max_age=86400)

@app.post("/api/films/<int:fid>/poster")
def set_poster(fid):
    c = C()
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    if request.content_length and request.content_length > POSTER_SRC_MAX + 64 * 1024:
        return jsonify(error="Image is too large (max 200 MB)"), 413
    up = request.files.get("file")
    if up:
        if (up.mimetype or "").lower() not in POSTER_TYPES: return jsonify(error="Only JPG, PNG or WebP images are allowed"), 415
        data = up.read(POSTER_SRC_MAX + 1)
        ext = sniff_image(data)
        if not ext: return jsonify(error="That file is not a valid JPG, PNG or WebP image"), 415
        if len(data) > POSTER_MAX:
            data, ext = fit_poster(data)
            if data is None: return jsonify(error=ext), 413
        val = _save_poster(c, fid, data, ext); note = "uploaded"
    else:
        url = ((request.json or {}).get("url") or "").strip() if request.is_json else ""
        if not url.lower().startswith(("http://", "https://")): return jsonify(error="Give an http(s) image URL or a file"), 400
        val = None; note = "linked (could not download, using the URL)"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 FilmTagger", "Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read(POSTER_MAX + 1)
            if len(data) > POSTER_MAX: return jsonify(error="Image is too large (max 15 MB)"), 413
            ext = sniff_image(data)
            if ext: val = _save_poster(c, fid, data, ext); note = "downloaded"
            elif data[:512].lstrip()[:1] == b"<": return jsonify(error="That URL is a web page, not an image"), 415
        except Exception:
            pass
        if val is None: _drop_local_poster(c, fid); val = url
    c.execute("UPDATE films SET custom_poster=?, updated_at=? WHERE id=?", (val, time.time(), fid)); c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note=note))

@app.delete("/api/films/<int:fid>/poster")
def remove_poster(fid):
    c = C(); _drop_local_poster(c, fid)
    c.execute("UPDATE films SET custom_poster=NULL, updated_at=? WHERE id=?", (time.time(), fid)); c.commit(); kick_focus()
    return get_film(fid)

# ---------- Find Posters: pick any TMDB poster for a mainstream movie (stored as a hand-set custom_poster) ----------
POSTER_BACKUP_DIR = os.path.join(POSTERS_DIR, "backup")
TMDB_IMG = "https://image.tmdb.org/t/p/"
FP_LANGS = ("en,null,de,fr,es,it,pt,nl,sv,da,no,fi,is,pl,cs,sk,hu,ro,bg,el,tr,ru,uk,sr,hr,sl,lt,lv,et,he,ar,fa,hi,"
            "th,vi,id,ms,ja,ko,zh,ca,eu,gl,ka")
import re as _fp_re
FP_PATH_RE = _fp_re.compile(r"^/[A-Za-z0-9_\-]+\.(jpg|jpeg|png|webp)$")

def fp_sort(posters):
    """English and no-language posters first, then by vote_average, then vote_count (both high first)."""
    return sorted(posters, key=lambda p: (0 if p.get("iso_639_1") in ("en", None, "") else 1,
                                          -(p.get("vote_average") or 0), -(p.get("vote_count") or 0)))

def _fp_prev_key(fid): return f"poster_prev:{fid}"

def _fp_backup_current(c, fid):
    """Keep the film's current custom poster: a local file is moved to posters/backup/ (never deleted).
    Returns the value to restore later (/posters/backup/<name>, a URL, or None for 'no custom poster')."""
    r = c.execute("SELECT custom_poster FROM films WHERE id=?", (fid,)).fetchone()
    v = (r and r[0]) or None
    if v and v.startswith("/posters/") and not v.startswith("/posters/backup/"):
        src = os.path.join(POSTERS_DIR, os.path.basename(v))
        if os.path.exists(src):
            os.makedirs(POSTER_BACKUP_DIR, exist_ok=True)
            name = os.path.basename(v)
            if os.path.exists(os.path.join(POSTER_BACKUP_DIR, name)): name = f"{int(time.time())}-{name}"
            os.replace(src, os.path.join(POSTER_BACKUP_DIR, name))
            return "/posters/backup/" + name
        return None
    return v

def _fp_restore_value(v):
    """Backup value -> custom_poster value (a backed-up local file is moved back into posters/)."""
    if v and v.startswith("/posters/backup/"):
        name = os.path.basename(v); src = os.path.join(POSTER_BACKUP_DIR, name)
        if not os.path.exists(src): return None, "The backed-up poster file is missing"
        os.makedirs(POSTERS_DIR, exist_ok=True)
        os.replace(src, os.path.join(POSTERS_DIR, name)); return "/posters/" + name, None
    return v, None

def _fp_movie(c, fid):
    r = c.execute("SELECT id, kind, tmdb_id FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return None, (jsonify(error="not found"), 404)
    if r["kind"] == "scene": return None, (jsonify(error="Find Posters is only for movies, not scenes"), 400)
    return r, None

def _fp_err(e):
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401: return "TMDB rejected the API key (401)", 502
        if e.code == 404: return "TMDB has no such movie (404)", 404
        if e.code == 429: return "TMDB is rate limiting; try again in a moment", 429
        return f"TMDB error {e.code}", 502
    return f"Network/TMDB error: {type(e).__name__}", 502  # never str(e): keep any URL (and key) out of replies/logs

@app.get("/api/films/<int:fid>/findposters")
def findposters(fid):
    """All TMDB posters for the film's existing TMDB id (one /movie/{id}/images request), best first."""
    c = C(); r, bad = _fp_movie(c, fid)
    if bad: return bad
    can_restore = db.get_setting(c, _fp_prev_key(fid)) is not None
    if not r["tmdb_id"]: return jsonify(matched=False, error="Not matched on TMDB", posters=[], can_restore=can_restore)
    key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    try: m = tmdb(f"movie/{r['tmdb_id']}/images", key, include_image_language=FP_LANGS)
    except Exception as e:
        msg, code = _fp_err(e); return jsonify(error=msg), code
    out = []
    for p in fp_sort(m.get("posters") or []):
        fp = p.get("file_path") or ""
        if not FP_PATH_RE.match(fp): continue
        out.append(dict(file_path=fp, thumb=TMDB_IMG + "w342" + fp, lang=p.get("iso_639_1") or None,
                        width=p.get("width"), height=p.get("height"),
                        vote_average=p.get("vote_average"), vote_count=p.get("vote_count")))
    return jsonify(matched=True, tmdb_id=r["tmdb_id"], posters=out, can_restore=can_restore)

def _fp_download(fp):
    """Original size first, then w780. Returns (bytes, ext) or (None, error)."""
    last = "Could not download that poster"
    for size in ("original", "w780"):
        try:
            req = urllib.request.Request(TMDB_IMG + size + fp, headers={"User-Agent": "FilmTagger", "Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=30) as resp: data = resp.read(POSTER_SRC_MAX + 1)
        except Exception as e:
            last = f"Could not download that poster ({type(e).__name__})"; continue
        if len(data) > POSTER_SRC_MAX: last = "Image is too large (max 200 MB)"; continue
        ext = sniff_image(data)
        if not ext: last = "TMDB did not return an image"; continue
        if len(data) > POSTER_MAX:
            data, ext = fit_poster(data)
            if data is None: last = ext; continue
        return data, ext
    return None, last

@app.post("/api/films/<int:fid>/findposters/set")
def findposters_set(fid):
    """{file_path:"/abc.jpg"} - download that TMDB poster and make it the film's custom (hand-set) poster.
    The previous custom poster is backed up and can be put back with /findposters/restore."""
    c = C(); r, bad = _fp_movie(c, fid)
    if bad: return bad
    if not r["tmdb_id"]: return jsonify(error="Not matched on TMDB"), 400
    fp = ((request.json or {}).get("file_path") or "").strip() if request.is_json else ""
    if not FP_PATH_RE.match(fp): return jsonify(error="Not a TMDB poster path"), 400
    data, ext = _fp_download(fp)
    if data is None: return jsonify(error=ext), 502
    prev = _fp_backup_current(c, fid)
    os.makedirs(POSTERS_DIR, exist_ok=True)  # same folder + naming as uploads (old file already moved to backup)
    name = f"film-{fid}-{int(time.time())}.{ext}"
    if os.path.exists(os.path.join(POSTERS_DIR, name)) or os.path.exists(os.path.join(POSTER_BACKUP_DIR, name)):
        name = f"film-{fid}-{int(time.time() * 1000)}.{ext}"
    with open(os.path.join(POSTERS_DIR, name), "wb") as fh: fh.write(data)
    val = "/posters/" + name
    db.set_setting(c, _fp_prev_key(fid), {"value": prev, "at": time.time()})
    ed = set(json.loads(c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()[0] or "[]")); ed.add("custom_poster")
    c.execute("UPDATE films SET custom_poster=?, edited_fields=?, updated_at=? WHERE id=?", (val, json.dumps(sorted(ed)), time.time(), fid))
    c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note="set from TMDB (previous poster backed up)", can_restore=True))

@app.post("/api/films/<int:fid>/findposters/restore")
def findposters_restore(fid):
    """Put back the poster that was there before the last Find Posters pick (the picked one is backed up too)."""
    c = C()
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    b = db.get_setting(c, _fp_prev_key(fid))
    if b is None: return jsonify(error="No previous poster to restore"), 400
    val, err = _fp_restore_value(b.get("value"))
    if err: return jsonify(error=err), 410
    _fp_backup_current(c, fid)  # the poster being replaced is kept in posters/backup too
    c.execute("DELETE FROM settings WHERE key=?", (_fp_prev_key(fid),))
    c.execute("UPDATE films SET custom_poster=?, updated_at=? WHERE id=?", (val, time.time(), fid)); c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note="previous poster restored", can_restore=False))

@app.get("/api/films/<int:fid>")
def get_film(fid):
    c = C(); r = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return jsonify(error="not found"), 404
    if r["kind"] == "scene" and request.method == "GET" and request.endpoint == "get_film":  # galleries added by hand show up without a scan
        try:
            scene_gallery(c, fid)  # cheap folder check; stores gallery_path/count only (updated_at untouched)
            r = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
        except Exception: pass
    return jsonify(attach(c, [dict(r)])[0])

def parse_tmdb_id(v):
    """Accepts 595334, '595334', or a TMDB URL like https://www.themoviedb.org/movie/595334-some-title."""
    if v in (None, ""): return None
    s = str(v).strip()
    m = _re.search(r"themoviedb\.org/(?:[a-z]{2}(?:-[A-Z]{2})?/)?movie/(\d+)", s) or _re.fullmatch(r"\s*(\d+)\s*", s)
    if not m: raise ValueError(f"Not a TMDB id or movie URL: {s}")
    return int(m.group(1))

def clean(d):
    out = {}
    for k in FILM_FIELDS:
        if k in d:
            v = d[k]
            if k == "tmdb_id": out[k] = parse_tmdb_id(v); continue
            if k in ("year", "runtime", "rating"):
                v = int(v) if v not in (None, "") else None
                if k == "rating" and v is not None: v = max(1, min(10, v))
            if k == "watched": v = 1 if v in (True, 1, "1", "true") else 0
            if k in ("country", "studio"): v = ", ".join(dict.fromkeys(split_list(v or ""))) or None
            if k == "studio": v = db.canon_studio(v)  # hidden studio aliases -> main name
            if k == "primary_genre": v = " ".join((v or "").split()) or None
            if k == "aznude_status": v = db.norm_aznude_status(v)
            if k == "subtitles": v = " ".join((v or "").split()) or None
            if k == "tpdb_id": v = tpdb.parse_id(v if isinstance(v, str) else str(v or ""))
            if k == "release_date":
                v = (v or "").strip() or None
                if v and not _re.fullmatch(r"(19|20)\d\d-\d\d-\d\d", v): raise ValueError(f"Release date must be YYYY-MM-DD, not {v}")
            if k == "resolution": v = resmod.normalize(v if isinstance(v, str) else "")
            if k == "user_akas":  # one title per line (semicolons also split); titles may contain commas
                parts = v if isinstance(v, list) else (v or "").replace(";", "\n").splitlines()
                seen = []
                for t in (" ".join(x.split()) for x in parts):
                    if t and t not in seen: seen.append(t)
                v = "\n".join(seen) or None
            out[k] = v
    return out

@app.post("/api/films")
def create_film():
    c = C(); d = request.json or {}; f = clean(d); f.setdefault("title", "Untitled")
    if f.get("primary_genre"):  # typed in the new-film window: remember it and protect it from auto fill
        f["primary_genre"] = db.remember_primary_genre(c, f["primary_genre"]); f["edited_fields"] = json.dumps(["primary_genre"])
    f["added_at"] = f["updated_at"] = time.time()
    cur = c.execute(f"INSERT INTO films({','.join(f)}) VALUES({','.join('?'*len(f))})", list(f.values()))
    for p in split_list(d.get("paths")): c.execute("INSERT OR IGNORE INTO files(film_id,path) VALUES(?,?)", (cur.lastrowid, p))
    c.commit()
    if split_list(d.get("paths")): start_res_job([cur.lastrowid])
    if f.get("tmdb_id"): fetch_and_apply(c, cur.lastrowid, f["tmdb_id"], keep=[k for k, v in f.items() if v not in (None, "") and k != "title"])
    return get_film(cur.lastrowid)

@app.put("/api/films/<int:fid>")
def update_film(fid):
    c = C(); d = request.json or {}
    try: f = clean(d)
    except ValueError as e: return jsonify(error=str(e)), 400
    old = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not old: return jsonify(error="not found"), 404
    ed = set(json.loads(old["edited_fields"] or "[]"))
    if f.get("primary_genre"):  # hand-typed primary genre: reuse the existing spelling, remember new names for the list
        f["primary_genre"] = db.remember_primary_genre(c, f["primary_genre"])
    changed = {k for k, v in f.items() if k != "tmdb_id" and (v or None) != (old[k] or None)}; ed |= changed
    if "subtitles" in changed: f["subs_manual"] = 1  # hand-edited subtitles: scans never overwrite until Re-detect
    if "aznude_status" in changed: f["aznude_checked"] = time.time()  # hand-set AZNude Status: protected from imports
    new_tid = f.get("tmdb_id") if "tmdb_id" in f and f.get("tmdb_id") and f.get("tmdb_id") != old["tmdb_id"] else None
    f["edited_fields"] = json.dumps(sorted(ed)); f["updated_at"] = time.time()
    c.execute(f"UPDATE films SET {','.join(k+'=?' for k in f)} WHERE id=?", list(f.values()) + [fid])
    paths_changed = False
    if "paths" in d:  # manual edit of the catalogue's path list (does not touch disk)
        new = split_list(d["paths"]) if not isinstance(d["paths"], str) else [x.strip() for x in d["paths"].splitlines() if x.strip()]
        before = {r[0] for r in c.execute("SELECT path FROM files WHERE film_id=?", (fid,))}
        c.execute(f"DELETE FROM files WHERE film_id=? AND path NOT IN ({','.join('?'*len(new)) or 'NULL'})", [fid] + new)
        if before != {r[0] for r in c.execute("SELECT path FROM files WHERE film_id=?", (fid,))}:
            c.execute("UPDATE films SET res_checked=NULL WHERE id=?", (fid,))  # paths changed: re-detect resolution
            c.commit(); start_res_job([fid]); paths_changed = True
        for p in new:
            c.execute("INSERT OR IGNORE INTO files(film_id,path,missing) VALUES(?,?,?)", (fid, p, 0 if os.path.exists(p) else 1))
    if paths_changed: detect_subs_one(c, fid)
    if d.get("replace_tags") is True and isinstance(d.get("tags"), list):  # explicit full replace of approved tags: list of {name, category}
        c.execute("DELETE FROM film_tags WHERE film_id=? AND status='approved'", (fid,))
        for t in d["tags"]:
            t = {"name": t} if isinstance(t, str) else t
            db.add_tag(c, fid, t.get("name"), t.get("category"))
    c.commit()
    if "poster" in changed and old["kind"] == "scene": kick_focus()
    rename_note = None
    if old["kind"] == "scene" and changed & set(SCENE_NAME_FIELDS):  # Rename on Save
        try: rename_note = rename_scene_file(c, fid)[1]
        except OSError as e: rename_note = f"Saved, but the file could not be renamed: {e}"
    if rename_note and not new_tid:
        j = get_film(fid).get_json(); j["rename_note"] = rename_note; return jsonify(j)
    if new_tid:  # new/changed TMDB id on Save: load its details now (hand edits + this save's changes are kept; tags only added)
        err = fetch_and_apply(c, fid, new_tid, keep=changed)
        r = get_film(fid); j = r.get_json(); j["tmdb_loaded"] = not err
        if err: j["tmdb_error"] = err
        return jsonify(j)
    return get_film(fid)

@app.delete("/api/films/<int:fid>")
def delete_film(fid):  # removes catalogue entry only; never touches files
    c = C()
    if request.args.get("exclude", "1") == "0": c.execute("DELETE FROM films WHERE id=?", (fid,))
    else: db.exclude_film(c, fid)  # remembered so scans never re-add it; the file itself is left alone
    c.commit(); return ok()

@app.post("/api/films/merge")
def merge_films():
    c = C(); ids = request.json["ids"]; keep = ids[0]
    for o in ids[1:]:
        c.execute("UPDATE files SET film_id=? WHERE film_id=?", (keep, o))
        c.execute("INSERT OR IGNORE INTO film_tags SELECT ?,tag_id,source,status,added_at FROM film_tags WHERE film_id=?", (keep, o))
        c.execute("DELETE FROM films WHERE id=?", (o,))
    c.commit(); return get_film(keep)

# ---------- tags ----------
@app.get("/api/tags")
def list_tags():
    c = C()
    rows = c.execute("SELECT t.id, t.name, t.category, (SELECT COUNT(*) FROM film_tags ft WHERE ft.tag_id=t.id AND ft.status='approved') n FROM tags t ORDER BY t.category IS NULL, t.category, t.name COLLATE FOLD")
    return jsonify([dict(r) for r in rows])

@app.put("/api/tags/<int:tid>")
def edit_tag(tid):
    c = C(); d = request.json or {}
    if "name" in d:
        other = c.execute("SELECT id FROM tags WHERE name=? AND id<>?", (d["name"].strip(), tid)).fetchone()
        if other:  # rename onto existing -> merge
            c.execute("INSERT OR IGNORE INTO film_tags SELECT film_id,?,source,status,added_at FROM film_tags WHERE tag_id=?", (other[0], tid))
            c.execute("DELETE FROM tags WHERE id=?", (tid,)); c.commit(); return ok(merged_into=other[0])
        c.execute("UPDATE tags SET name=? WHERE id=?", (d["name"].strip(), tid))
    if "category" in d:
        c.execute("UPDATE tags SET category=? WHERE id=?", (d["category"] or None, tid))
        if d["category"]: c.execute("INSERT OR IGNORE INTO categories(name,sort) VALUES(?,99)", (d["category"],))
    c.commit(); return ok()

@app.delete("/api/tags/<int:tid>")
def del_tag(tid):
    c = C(); c.execute("DELETE FROM tags WHERE id=?", (tid,)); c.commit(); return ok()

@app.post("/api/bulk/tags")
def bulk_tags():
    """{film_ids:[..], add:[{name,category}], remove:[names]}"""
    c = C(); d = request.json or {}
    for fid in d.get("film_ids", []):
        for t in d.get("add", []):
            t = {"name": t} if isinstance(t, str) else t
            db.add_tag(c, fid, t.get("name"), t.get("category"))
        for name in d.get("remove", []):
            c.execute("DELETE FROM film_tags WHERE film_id=? AND tag_id=(SELECT id FROM tags WHERE name=?)", (fid, name))
        c.execute("UPDATE films SET updated_at=? WHERE id=?", (time.time(), fid))
    c.commit(); return ok()

@app.post("/api/bulk/update")
def bulk_update():
    c = C(); d = request.json or {}; f = clean(d.get("fields", {}))
    for fid in d.get("film_ids", []):
        if f: c.execute(f"UPDATE films SET {','.join(k+'=?' for k in f)} WHERE id=?", list(f.values()) + [fid])
    c.commit(); return ok()

@app.post("/api/bulk/resolution")
def bulk_resolution():
    """{film_ids:[..], resolution:"1080p"|""} - set/clear resolution on many films (marked hand-edited)."""
    d = request.json or {}
    try: ids = [int(i) for i in d.get("film_ids", [])]; v = resmod.normalize(d.get("resolution") or "")
    except (TypeError, ValueError) as e: return jsonify(error=str(e) or "bad input"), 400
    c = C(); now = time.time(); n = 0
    try:
        for fid in ids:
            r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
            if not r: continue
            ed = set(json.loads(r[0] or "[]")); ed.add("resolution")
            c.execute("UPDATE films SET resolution=?, edited_fields=?, updated_at=? WHERE id=?", (v, json.dumps(sorted(ed)), now, fid)); n += 1
        c.commit()
    except Exception as e:
        c.rollback(); return jsonify(error=str(e)), 500
    return ok(updated=n)

@app.post("/api/bulk/mpaa")
def bulk_mpaa():
    """{film_ids:[..], mpaa:"X"|""} - set/clear MPAA rating on many films in one transaction."""
    d = request.json or {}
    try: ids = [int(i) for i in d.get("film_ids", [])]
    except (TypeError, ValueError): return jsonify(error="bad film_ids"), 400
    v = (d.get("mpaa") or "").strip() or None
    c = C(); now = time.time(); n = 0
    try:
        for fid in ids:
            r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
            if not r: continue
            ed = set(json.loads(r[0] or "[]")); ed.add("mpaa")
            c.execute("UPDATE films SET mpaa=?, edited_fields=?, updated_at=? WHERE id=?", (v, json.dumps(sorted(ed)), now, fid)); n += 1
        c.commit()
    except Exception as e:
        c.rollback(); return jsonify(error=str(e)), 500
    return ok(updated=n)

@app.post("/api/bulk/delete")
def bulk_delete():
    c = C()
    for fid in (request.json or {}).get("film_ids", []): db.exclude_film(c, fid)
    c.commit(); return ok()

@app.get("/api/excluded")
def excluded_list():
    c = C(); sr = db.scene_roots(c)
    rows = c.execute("SELECT id, path, size, title, kind, removed_at, oshash IS NOT NULL hashed, studio, cast_ FROM excluded").fetchall()
    return jsonify(items=sort_excluded([excluded_item(dict(r), sr) for r in rows]))

def excluded_item(r, sr):
    """Studio and performers: stored at removal, else from the <Studio> folder / file name (scene roots only)."""
    import tpdb
    in_scenes = any(db.under(r["path"], x) for x in sr)
    n = tpdb.parse_name(r["path"]) if in_scenes else {}
    st = (r.get("studio") or "").strip() or (db.folder_studio(r["path"], sr) if in_scenes else None) or n.get("studio") or ""
    if st.startswith("_"): st = ""  # '_No Studio' / '_Duplicates' folders are not studios
    perf = [x.strip() for x in (r.get("cast_") or "").split(",") if x.strip()] or n.get("performers") or []
    r.update(studio=st, performers=perf, exists=os.path.exists(r["path"])); r.pop("cast_", None)
    return r

def sort_excluded(items):
    """Studio A-Z (no studio last), then first performer A-Z (none last), then newest removal first."""
    return sorted(items, key=lambda x: (not x["studio"], db.fold(x["studio"]), not x["performers"],
                                        db.fold(x["performers"][0]) if x["performers"] else "", -(x["removed_at"] or 0)))

@app.post("/api/excluded/<int:xid>/restore")
def excluded_restore(xid):
    """Forget the exclusion and add the file back right away if it is still there (title is guessed again / rematched)."""
    c = C(); r = c.execute("SELECT * FROM excluded WHERE id=?", (xid,)).fetchone()
    if not r: return jsonify(error="not found"), 404
    c.execute("DELETE FROM excluded WHERE id=?", (xid,)); c.commit()
    added = False
    roots = [x for x in (db.get_setting(c, "roots", []) or []) + db.scene_roots(c) if x]
    root = next((x for x in roots if db.under(r["path"], x)), None)
    if root and os.path.exists(r["path"]):
        res = db.scan(c, [root]); added = res.get("added", 0) > 0
    return ok(added=added)

@app.get("/api/categories")
def cats():
    return jsonify([r[0] for r in C().execute("SELECT name FROM categories ORDER BY sort, name COLLATE FOLD")])

@app.put("/api/categories")
def set_cats():
    c = C(); names = [n.strip() for n in request.json if n.strip()]
    c.execute("DELETE FROM categories")
    c.executemany("INSERT OR IGNORE INTO categories VALUES(?,?)", [(n, i) for i, n in enumerate(names)])
    c.commit(); return ok()

# ---------- smart tag queue ----------
@app.post("/api/queue")
def queue_add():
    c = C()
    for fid in (request.json or {}).get("film_ids", []):
        c.execute("INSERT OR IGNORE INTO smart_queue VALUES(?,?)", (fid, time.time()))
    c.commit(); return ok(count=c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0])

@app.delete("/api/queue")
def queue_remove():
    c = C(); ids = (request.json or {}).get("film_ids")
    if ids is None: c.execute("DELETE FROM smart_queue")
    else:
        for fid in ids: c.execute("DELETE FROM smart_queue WHERE film_id=?", (fid,))
    c.commit(); return ok()

# ---------- AZNude Status queue (checked outside the app; never changes primary_genre or tags) ----------
def aznude_count(c): return c.execute("SELECT COUNT(*) FROM aznude_queue").fetchone()[0]

@app.post("/api/aznude/queue")
def aznude_queue_add():
    c = C()
    for fid in (request.json or {}).get("film_ids", []):
        c.execute("INSERT OR IGNORE INTO aznude_queue SELECT id, ? FROM films WHERE id=?", (time.time(), fid))
    c.commit(); return ok(count=aznude_count(c))

@app.delete("/api/aznude/queue")
def aznude_queue_remove():
    c = C(); ids = (request.json or {}).get("film_ids") if request.is_json else None
    if ids is None: c.execute("DELETE FROM aznude_queue")
    else:
        for fid in ids: c.execute("DELETE FROM aznude_queue WHERE film_id=?", (fid,))
    c.commit(); return ok(count=aznude_count(c))

@app.get("/api/aznude/queue")
def aznude_queue_export(): return jsonify(db.export_aznude_queue(C()))

@app.post("/api/aznude/import")
def aznude_import():
    d = request.json
    if isinstance(d, dict): d = d.get("items", d.get("results", [d]))
    return jsonify(db.import_aznude(C(), d or []))

@app.post("/api/films/<int:fid>/aznude")
def aznude_set(fid):
    """Set one film's AZNude Status. Body: {status: "Hardcore"|"Softcore"|"Not found"|null, url?, note?,
    manual?: true marks it hand-edited, force?: true overrides a hand edit}. Removes the film from the AZNude queue."""
    c = C(); d = request.json or {}
    try: r = db.set_aznude(c, fid, d.get("status"), d.get("url"), d.get("note"), manual=bool(d.get("manual")), force=bool(d.get("force")))
    except ValueError as e: return jsonify(error=str(e)), 400
    if r == "missing": return jsonify(error="not found"), 404
    c.execute("DELETE FROM aznude_queue WHERE film_id=?", (fid,)); c.commit()
    j = get_film(fid).get_json(); j["aznude_result"] = r
    return jsonify(j)

@app.get("/api/queue/export")
def queue_export(): return jsonify(db.export_queue(C()))

@app.post("/api/queue/import")
def queue_import():
    d = request.json
    if isinstance(d, dict): d = d.get("items", [d])
    return jsonify(db.import_ai_tags(C(), d))

@app.get("/api/pending")
def pending():
    c = C(); rows = [dict(r) for r in c.execute("SELECT DISTINCT f.* FROM films f JOIN film_tags ft ON ft.film_id=f.id WHERE ft.status='pending' ORDER BY f.title COLLATE FOLD")]
    return jsonify(attach(c, rows))

@app.post("/api/pending/review")
def review():
    """{film_id?, tag_ids?:[..], action:'approve'|'reject'}; omit film_id to apply to all pending."""
    c = C(); d = request.json or {}; act = d.get("action")
    w, a = ["status='pending'"], []
    if d.get("film_id") is not None: w.append("film_id=?"); a.append(d["film_id"])
    if d.get("tag_ids"): w.append(f"tag_id IN ({','.join('?'*len(d['tag_ids']))})"); a += d["tag_ids"]
    sql = ("UPDATE film_tags SET status='approved' WHERE " if act == "approve" else "DELETE FROM film_tags WHERE ") + " AND ".join(w)
    n = c.execute(sql, a).rowcount
    c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM film_tags)") if act == "reject" else None
    c.commit(); return ok(changed=n)

# ---------- scan / settings / saved searches ----------
@app.get("/api/settings")
def settings_get():
    c = C()
    ak = db.get_setting(c, "ai_key", "")
    return jsonify(roots=db.get_setting(c, "roots", []), tmdb_key=db.get_setting(c, "tmdb_key", ""),
                   ai_url=db.get_setting(c, "ai_url", ""), ai_model=db.get_setting(c, "ai_model", ""),
                   ai_key_set=bool(ak), scene_roots=db.get_setting(c, "scene_roots", []),
                   tpdb_key_set=bool(db.get_setting(c, "tpdb_key", "")), ai_key_hint=("…" + ak[-4:]) if len(ak) > 8 else ("set" if ak else ""),
                   queue=c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0],
                   pending=c.execute("SELECT COUNT(DISTINCT film_id) FROM film_tags WHERE status='pending'").fetchone()[0],
                   films=c.execute("SELECT COUNT(*) FROM films").fetchone()[0], db_path=db.DB_PATH)

@app.put("/api/settings")
def settings_put():
    c = C(); d = request.json or {}
    if "roots" in d: db.set_setting(c, "roots", [r.strip() for r in d["roots"] if r.strip()])
    if "tmdb_key" in d: db.set_setting(c, "tmdb_key", d["tmdb_key"].strip().strip('"').strip("'").strip())
    if "ai_url" in d: db.set_setting(c, "ai_url", d["ai_url"].strip())
    if "scene_roots" in d: db.set_setting(c, "scene_roots", [r.strip().rstrip("\\/") for r in d["scene_roots"] if r.strip()])
    if d.get("tpdb_key") is not None and d.get("tpdb_key") != "":  # blank = keep existing; "-" = clear
        db.set_setting(c, "tpdb_key", "" if d["tpdb_key"].strip() == "-" else d["tpdb_key"].strip().strip('"').strip("'"))
    if "ai_model" in d: db.set_setting(c, "ai_model", d["ai_model"].strip())
    if d.get("ai_key") is not None and d.get("ai_key") != "":  # blank = keep existing; "-" = clear
        db.set_setting(c, "ai_key", "" if d["ai_key"].strip() == "-" else d["ai_key"].strip().strip('"').strip("'"))
    c.commit(); return settings_get()

@app.post("/api/scan")
def scan():
    c = C(); roots = (request.json or {}).get("roots") or db.get_setting(c, "roots", [])
    if SCAN["running"]: return jsonify(error="A scan is already running"), 409
    SCAN.clear(); SCAN.update(running=True, kind="scan", found=0, added=0, cancel=False, current="")
    try:
        r = db.scan(c, roots, SCAN); SCAN.update(r); SCAN["cancelled"] = SCAN.get("cancel", False)
        threading.Thread(target=refresh_galleries, daemon=True).start()
        return jsonify(r)
    finally:
        SCAN["running"] = False; SCAN["finished_at"] = time.time()
        try: start_res_job()  # detect resolution of newly found films in the background
        except Exception: pass
        try: start_tpdb_job()  # look up new scenes on ThePornDB (only if a token is set)
        except Exception: pass
        try: start_subs_job(recheck=True)  # refresh subtitles for every non-overridden film (cached probes: fast)
        except Exception: pass

SCAN = {"running": False, "kind": "scan"}

@app.post("/api/scan/cancel")
def scan_cancel(): SCAN["cancel"] = True; return jsonify(SCAN)

@app.get("/api/libraries")
def libraries():
    """Configured roots plus top-level subfolders (derived from catalogued paths, so works offline)."""
    c = C(); sr = db.scene_roots(c); out = []
    roots = [r for r in db.get_setting(c, "roots", []) if not any(os.path.normcase(r.rstrip("\\/")) == os.path.normcase(x) for x in sr)]
    paths = [r[0] for r in c.execute("SELECT path FROM files")]
    if sr: roots.append(sr[0])  # every scene root shows as ONE library under the first root's path
    for root in roots:
        rp = root.rstrip("\\/"); low = rp.lower(); subs = {}; n = 0
        for p in paths:
            hit = next((x for x in (sr if sr and root == sr[0] else [rp]) if db.under(p, x)), None)
            if hit:
                n += 1; rest = p[len(hit) + 1:]; parts = _re.split(r"[\\/]", rest)
                if len(parts) > 1: subs[parts[0].lower()] = (parts[0], subs.get(parts[0].lower(), (0, 0))[1] + 1)
        sep = "\\" if "\\" in rp or _re.match(r"^[A-Za-z]:", rp) else "/"
        out.append(dict(path=rp, count=n, roots=(sr if sr and root == sr[0] else [rp]), subfolders=sorted([dict(name=v[0], path=rp + sep + v[0], count=v[1]) for v in subs.values()], key=lambda x: x["name"].lower())))
    return jsonify(out)

@app.get("/api/jobs")
def jobs(): return jsonify(job=JOB, scan=SCAN, res=RES, subs=SUBS)

@app.get("/api/searches")
def searches(): return jsonify([dict(id=r[0], name=r[1], query=json.loads(r[2])) for r in C().execute("SELECT * FROM saved_searches ORDER BY name COLLATE FOLD")])

@app.post("/api/searches")
def save_search():
    c = C(); d = request.json
    c.execute("DELETE FROM saved_searches WHERE name=?", (d["name"],))
    c.execute("INSERT INTO saved_searches(name,query) VALUES(?,?)", (d["name"], json.dumps(d["query"]))); c.commit(); return ok()

@app.delete("/api/searches/<int:sid>")
def del_search(sid):
    c = C(); c.execute("DELETE FROM saved_searches WHERE id=?", (sid,)); c.commit(); return ok()

# ---------- native folder picker (local machine only) ----------
import subprocess, sys
TK_PICK = r"""
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    sys.exit(3)
try:
    r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
    p = filedialog.askdirectory(parent=r, title='Choose a film folder', mustexist=True)
    r.destroy()
except Exception:
    sys.exit(3)
sys.stdout.write(p or '')
"""
PS_PICK = ("Add-Type -AssemblyName System.Windows.Forms;"
           "$f=New-Object System.Windows.Forms.FolderBrowserDialog;$f.Description='Choose a film folder';"
           "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
           "if($f.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($f.SelectedPath)}")

@app.post("/api/pick-folder")
def pick_folder():
    """Opens a native folder chooser in a fresh subprocess; returns {path: str|null}."""
    path, err = None, None
    try:
        r = subprocess.run([sys.executable, "-c", TK_PICK], capture_output=True, timeout=600)
        if r.returncode == 0:
            path = r.stdout.decode("utf-8", "replace").strip() or None
            return jsonify(path=os.path.normpath(path) if path else None)
        err = "tkinter unavailable"
    except Exception as e: err = str(e)
    if os.name == "nt":
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", PS_PICK],
                               capture_output=True, timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            path = r.stdout.decode("utf-8", "replace").strip() or None
            return jsonify(path=path)
        except Exception as e: err = str(e)
    return jsonify(path=None, error=f"No folder picker available ({err}); type the path instead")

# ---------- TMDB (optional) ----------
def tmdb(path, key, **params):
    if path.startswith("search/"): params.setdefault("include_adult", "true")
    key = (key or "").strip().strip('"').strip("'").strip()
    if key.lower().startswith("bearer "): key = key[7:].strip()
    headers = {"Accept": "application/json"}
    if len(key) > 40: headers["Authorization"] = "Bearer " + key  # v4 Read Access Token
    else: params["api_key"] = key
    url = f"https://api.themoviedb.org/3/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r: return json.load(r)

def us_cert(m):
    """US certification from TMDB release_dates (prefers theatrical, then any non-empty)."""
    for r in (m.get("release_dates") or {}).get("results", []):
        if r.get("iso_3166_1") == "US":
            rds = sorted(r.get("release_dates", []), key=lambda x: (x.get("type") != 3, x.get("type") or 9))
            for x in rds:
                if (x.get("certification") or "").strip(): return x["certification"].strip()
    return None

def alt_titles_of(m):
    a = m.get("alternative_titles") or {}
    a = a.get("titles", a.get("results", [])) if isinstance(a, dict) else a
    seen = []
    for x in a:
        t = (x.get("title") or "").strip()
        if t and t != m.get("title") and t not in seen: seen.append(t)
    return "; ".join(seen)

def countries_of(m):
    return ", ".join(dict.fromkeys((x.get("name") or "").strip() for x in (m.get("production_countries") or []) if (x.get("name") or "").strip()))

def studios_of(m):
    return ", ".join(dict.fromkeys((x.get("name") or "").strip() for x in (m.get("production_companies") or []) if (x.get("name") or "").strip()))

# Columns TMDB code must never write, whatever the caller asks for (user data / user art / local detection).
TMDB_NEVER = {"kind", "release_date", "tpdb_id", "tpdb_checked", "rating", "watched", "notes", "custom_poster", "storage", "user_akas", "resolution", "subtitles", "subs_manual", "subs_checked", "edited_fields",
              "aznude_status", "aznude_checked", "aznude_url", "aznude_note"}

def apply_movie(c, fid, m, respect_edits=True, genres=True, keep=()):
    """Apply TMDB details to a film. Never removes or alters existing tags (approved or pending): genres are only
    added when the film does not already have that tag. Hand-edited fields (edited_fields) are never replaced,
    even on an explicit fetch; `respect_edits` is kept for API compatibility and cannot turn that off."""
    crew = m.get("credits", {})
    def jobs(*js):
        seen = []
        for x in crew.get("crew", []):
            if x.get("job") in js and x.get("name") and x["name"] not in seen: seen.append(x["name"])
        return ", ".join(seen)
    upd = dict(title=m.get("title"), year=int(m["release_date"][:4]) if m.get("release_date") else None,
               director=", ".join(x["name"] for x in crew.get("crew", []) if x.get("job") == "Director"),
               cast_=", ".join(x["name"] for x in crew.get("cast", [])[:10]), runtime=m.get("runtime"),
               plot=m.get("overview"),
               writers=jobs("Screenplay", "Writer", "Story", "Novel", "Author"),
               producers=jobs("Producer", "Executive Producer"),
               cinematographer=jobs("Director of Photography"),
               composer=jobs("Original Music Composer", "Music", "Composer"),
               poster=m.get("poster_path") and "https://image.tmdb.org/t/p/w342" + m["poster_path"],
               mpaa=us_cert(m))
    ot = (m.get("original_title") or "").strip()
    if ot and ot != (m.get("title") or "").strip(): upd["original_title"] = ot
    if "alternative_titles" in m: upd["alt_titles"] = alt_titles_of(m)
    upd = {k: v for k, v in upd.items() if v not in (None, "")}
    if "release_dates" in m: upd["mpaa_checked"] = 1
    if "production_countries" in m:
        if countries_of(m): upd["country"] = countries_of(m)
        upd["country_checked"] = 1
    if "production_companies" in m:
        if studios_of(m): upd["studio"] = db.canon_studio(studios_of(m), c)
        upd["studio_checked"] = 1
    ed = set(json.loads((c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or "[]"))
    upd = {k: v for k, v in upd.items() if k not in ed and k not in TMDB_NEVER and k not in set(keep)}
    upd["tmdb_id"] = m["id"]; upd["tmdb_status"] = "matched"
    if upd.get("title"): upd["title"] = db.strip_title_quotes(upd["title"])
    upd["updated_at"] = time.time()
    gnames = [g.get("name") for g in m.get("genres", []) if g.get("name")]
    if "genres" in m: upd["tmdb_genres"] = json.dumps(gnames, ensure_ascii=False)
    c.execute(f"UPDATE films SET {','.join(k+'=?' for k in upd)} WHERE id=?", list(upd.values()) + [fid])
    if genres:
        for g in m.get("genres", []): db.add_tag_if_absent(c, fid, g.get("name"), "Genre", source="tmdb")
    if "primary_genre" not in ed and "primary_genre" not in set(keep):  # only fills an empty value
        if db.is_classic_porn(c, fid): db.set_porn_primary_genre(c, fid)  # never a TMDB genre for Classic Porn
        elif gnames: db.set_primary_genre(c, fid, gnames[0], only_if_empty=True)

# ---------- bulk poster/metadata job ----------
import threading, re as _re, unicodedata
JOB = {"running": False, "kind": "posters", "done": 0, "total": 0, "matched": 0, "skipped": 0, "errors": 0, "error": None, "cancel": False}
JOB_LOCK = threading.Lock()

def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = _re.sub(r"^(the|a|an)\s+", "", _re.sub(r"[^a-z0-9 ]+", " ", s.replace("&", "and")))
    return " ".join(s.split())

def pick_confident(title, year, results):
    def yr(r): return int(r["release_date"][:4]) if (r.get("release_date") or "")[:4].isdigit() else None
    if len(results) == 1:
        r = results[0]
        if year is None or yr(r) is None or abs(yr(r) - year) <= 1: return r
        return None
    for r in results[:5]:
        if _norm(r.get("title")) in (_norm(title),) or _norm(r.get("original_title")) == _norm(title):
            if year is not None and yr(r) is not None and abs(yr(r) - year) <= 1: return r
            if year is None and sum(_norm(x.get("title")) == _norm(title) for x in results) == 1: return r
    return None

class RateLimiter:
    def __init__(self, per_sec): self.gap = 1.0 / per_sec; self.next = 0
    def wait(self):
        now = time.monotonic()
        if now < self.next: time.sleep(self.next - now)
        self.next = max(now, self.next) + self.gap

def run_poster_job(key, ids):
    rl = RateLimiter(20); c = C()
    def call(path, **p): rl.wait(); return tmdb(path, key, **p)
    try:
        for fid in ids:
            if JOB["cancel"]: break
            f = c.execute("SELECT title, year FROM films WHERE id=?", (fid,)).fetchone()
            if not f: JOB["done"] += 1; continue
            try:
                p = {"query": f["title"]}
                if f["year"]: p["year"] = f["year"]
                res = call("search/movie", **p).get("results", [])
                if not res and f["year"]: res = call("search/movie", query=f["title"]).get("results", [])
                r = pick_confident(f["title"], f["year"], res)
                if r:
                    apply_movie(c, fid, call(f"movie/{r['id']}", append_to_response="credits,release_dates,alternative_titles", include_adult="true"))
                    JOB["matched"] += 1
                else:
                    c.execute("UPDATE films SET tmdb_status='needs_match' WHERE id=?", (fid,)); JOB["skipped"] += 1
                c.commit()
            except urllib.error.HTTPError as e:
                if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; break
                JOB["errors"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"
                if JOB["errors"] >= 5 and JOB["matched"] == 0: JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

def run_mpaa_job(key, ids):
    rl = RateLimiter(20); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            try:
                rl.wait(); m = tmdb(f"movie/{tid}/release_dates", key)
                cert = us_cert({"release_dates": m})
                ed = json.loads(c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()[0] or "[]")
                if cert and "mpaa" not in ed:
                    c.execute("UPDATE films SET mpaa=?, mpaa_checked=1 WHERE id=? AND (mpaa IS NULL OR mpaa='')", (cert, fid)); JOB["matched"] += 1
                else:
                    c.execute("UPDATE films SET mpaa_checked=1 WHERE id=?", (fid,)); JOB["skipped"] += 1
                c.commit()
            except urllib.error.HTTPError as e:
                if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; break
                JOB["errors"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"
                if JOB["errors"] >= 5 and JOB["matched"] == 0: JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

@app.post("/api/mpaa/start")
def mpaa_start():
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        retry = (request.json or {}).get("retry_checked", False)
        ids = [(r[0], r[1]) for r in c.execute("SELECT id, tmdb_id FROM films WHERE tmdb_id IS NOT NULL AND (mpaa IS NULL OR mpaa='')"
               + ("" if retry else " AND mpaa_checked IS NULL") + " AND COALESCE(edited_fields,'') NOT LIKE '%\"mpaa\"%' ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="mpaa", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_mpaa_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

def run_credits_job(key, ids):
    rl = RateLimiter(20); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            try:
                rl.wait(); m = tmdb(f"movie/{tid}", key, append_to_response="credits")
                # only people fields; respects the user's manual edits
                ed = set(json.loads(c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()[0] or "[]"))
                keep = [k for k in FILM_FIELDS if k not in ("director", "cast_", *db.PEOPLE_COLS)]
                apply_movie(c, fid, m, respect_edits=True, genres=False, keep=keep)
                c.commit(); JOB["matched"] += 1
            except urllib.error.HTTPError as e:
                if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; break
                JOB["errors"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"
                if JOB["errors"] >= 5 and JOB["matched"] == 0: JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

def run_titles_job(key, ids):
    """Backfill original_title / alt_titles only. Never writes any other column."""
    rl = RateLimiter(20); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            try:
                rl.wait(); m = tmdb(f"movie/{tid}", key, append_to_response="alternative_titles")
                row = c.execute("SELECT title, edited_fields FROM films WHERE id=?", (fid,)).fetchone()
                ed = set(json.loads(row["edited_fields"] or "[]"))
                ot = (m.get("original_title") or "").strip()
                if "original_title" not in ed and ot and ot != (m.get("title") or "").strip() and ot != (row["title"] or "").strip():
                    c.execute("UPDATE films SET original_title=? WHERE id=?", (ot, fid)); JOB["matched"] += 1
                else: JOB["skipped"] += 1
                c.execute("UPDATE films SET alt_titles=? WHERE id=?", (alt_titles_of(m), fid))
                c.commit()
            except urllib.error.HTTPError as e:
                if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; break
                JOB["errors"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"
                if JOB["errors"] >= 5 and JOB["matched"] == 0: JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

def run_country_job(key, ids):
    """Backfill films.country from TMDB production_countries. Never overwrites a hand-edited or existing value."""
    rl = RateLimiter(8); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            try:
                rl.wait(); m = tmdb(f"movie/{tid}", key, include_adult="true")
                cn = countries_of(m)
                cur = c.execute("UPDATE films SET country=? WHERE id=? AND (country IS NULL OR country='') AND COALESCE(edited_fields,'') NOT LIKE '%\"country\"%'", (cn, fid)) if cn else None
                if cur is not None and cur.rowcount: JOB["matched"] += 1
                else: JOB["skipped"] += 1
                c.execute("UPDATE films SET country_checked=1 WHERE id=?", (fid,)); c.commit()
            except urllib.error.HTTPError as e:
                if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; break
                if e.code == 429: time.sleep(5)
                JOB["errors"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"
                if JOB["errors"] >= 5 and JOB["matched"] == 0: JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

@app.post("/api/country/start")
def country_start():
    """Backfill country for films with a tmdb_id and no country. {retry_checked: true} also retries films TMDB had none for."""
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        retry = (request.json or {}).get("retry_checked", False) if request.is_json else False
        ids = [(r[0], r[1]) for r in c.execute("SELECT id, tmdb_id FROM films WHERE tmdb_id IS NOT NULL AND (country IS NULL OR country='')"
               + ("" if retry else " AND country_checked IS NULL") + " AND COALESCE(edited_fields,'') NOT LIKE '%\"country\"%' ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="country", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_country_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

def run_studio_job(key, ids):
    """Backfill films.studio from TMDB production_companies. Never overwrites a hand-edited or existing value."""
    rl = RateLimiter(8); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            try:
                rl.wait(); m = tmdb(f"movie/{tid}", key, include_adult="true")
                st = db.canon_studio(studios_of(m), c)
                cur = c.execute("UPDATE films SET studio=? WHERE id=? AND (studio IS NULL OR studio='') AND COALESCE(edited_fields,'') NOT LIKE '%\"studio\"%'", (st, fid)) if st else None
                if cur is not None and cur.rowcount: JOB["matched"] += 1
                else: JOB["skipped"] += 1
                c.execute("UPDATE films SET studio_checked=1 WHERE id=?", (fid,)); c.commit()
            except urllib.error.HTTPError as e:
                if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; break
                if e.code == 429: time.sleep(5)
                JOB["errors"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"
                if JOB["errors"] >= 5 and JOB["matched"] == 0: JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

@app.post("/api/studio/start")
def studio_start():
    """Backfill studio for films with a tmdb_id and no studio. {retry_checked: true} also retries films TMDB had none for."""
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        retry = (request.json or {}).get("retry_checked", False) if request.is_json else False
        ids = [(r[0], r[1]) for r in c.execute("SELECT id, tmdb_id FROM films WHERE tmdb_id IS NOT NULL AND (studio IS NULL OR studio='')"
               + ("" if retry else " AND studio_checked IS NULL") + " AND COALESCE(edited_fields,'') NOT LIKE '%\"studio\"%' ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="studio", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False, started_at=time.time())
        threading.Thread(target=run_studio_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

@app.get("/api/studio/status")
def studio_status():
    c = C(); q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE " + w).fetchone()[0]
    j = dict(JOB); eta = None
    if j.get("kind") == "studio" and j.get("running") and j.get("done") and j.get("started_at"):
        eta = round((time.time() - j["started_at"]) / j["done"] * (j["total"] - j["done"]))
    return jsonify(job=j, eta_seconds=eta, with_studio=q("studio IS NOT NULL AND studio<>''"),
                   remaining=q("tmdb_id IS NOT NULL AND (studio IS NULL OR studio='') AND studio_checked IS NULL"),
                   no_tmdb_studio=q("tmdb_id IS NOT NULL AND (studio IS NULL OR studio='') AND studio_checked=1"))

PG_BLANK = "COALESCE(primary_genre,'')='' AND COALESCE(edited_fields,'') NOT LIKE '%\"primary_genre\"%'"
PG_CLASSIC = "id IN (SELECT film_id FROM files WHERE " + db.CLASSIC_PORN_SQL + ")"
PG_WHERE = "tmdb_id IS NOT NULL AND " + PG_BLANK + " AND NOT " + PG_CLASSIC  # Classic Porn never gets TMDB genres

def run_pgenre_job(key, ids):
    """Fill empty, non-hand-edited primary_genre from TMDB's genre list. Only writes tmdb_genres and primary_genre:
    never adds/removes/changes tags or any other metadata. ~4 requests/s, backs off and retries on 429."""
    rl = RateLimiter(4); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            for attempt in range(4):
                try:
                    rl.wait(); m = tmdb(f"movie/{tid}", key, include_adult="true")
                    names = [g.get("name") for g in m.get("genres", []) if g.get("name")]
                    c.execute("UPDATE films SET tmdb_genres=? WHERE id=?", (json.dumps(names, ensure_ascii=False), fid))
                    if names and db.set_primary_genre(c, fid, names[0], only_if_empty=True): JOB["matched"] += 1
                    else: JOB["skipped"] += 1
                    c.commit(); break
                except urllib.error.HTTPError as e:
                    if e.code == 429 and attempt < 3:
                        time.sleep(float(e.headers.get("Retry-After") or 0) or 2 * (attempt + 1)); continue
                    if e.code == 401: JOB["error"] = "TMDB rejected the API key (401)"; JOB["cancel"] = True
                    JOB["errors"] += 1; break
                except Exception as e:
                    JOB["errors"] += 1; JOB["error"] = f"Network/TMDB error: {e}"; break
            if JOB["errors"] >= 10 and JOB["matched"] == 0: JOB["error"] = (JOB["error"] or "") + " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

@app.post("/api/pgenre/start")
def pgenre_start():
    """Fill missing primary genres from TMDB for films with a tmdb_id, empty primary_genre and no hand edit."""
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        for (pid,) in c.execute("SELECT id FROM films WHERE " + PG_BLANK + " AND " + PG_CLASSIC).fetchall():
            db.set_porn_primary_genre(c, pid)  # Classic Porn: Hardcore/Softcore from tags, no TMDB
        c.commit()
        ids = [(r[0], r[1]) for r in c.execute("SELECT id, tmdb_id FROM films WHERE " + PG_WHERE + " ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="pgenre", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False, started_at=time.time())
        threading.Thread(target=run_pgenre_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

@app.get("/api/pgenre/status")
def pgenre_status():
    c = C(); q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE " + w).fetchone()[0]
    return jsonify(job=dict(JOB), with_primary_genre=q("COALESCE(primary_genre,'')<>''"), blank=q("COALESCE(primary_genre,'')=''"),
                   remaining=q(PG_WHERE), no_tmdb_id=q("tmdb_id IS NULL AND COALESCE(primary_genre,'')=''"))

@app.post("/api/bulk/studio")
def bulk_studio():
    """{film_ids:[..], studio:"A, B"|""} - set/clear studio on many films (marked hand-edited)."""
    d = request.json or {}
    try: ids = [int(i) for i in d.get("film_ids", [])]
    except (TypeError, ValueError): return jsonify(error="bad film_ids"), 400
    v = ", ".join(dict.fromkeys(split_list(d.get("studio") or ""))) or None
    c = C(); now = time.time(); n = 0; v = db.canon_studio(v, c)
    try:
        for fid in ids:
            r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
            if not r: continue
            ed = set(json.loads(r[0] or "[]")); ed.add("studio")
            c.execute("UPDATE films SET studio=?, edited_fields=?, updated_at=? WHERE id=?", (v, json.dumps(sorted(ed)), now, fid)); n += 1
        c.commit()
    except Exception as e:
        c.rollback(); return jsonify(error=str(e)), 500
    return ok(updated=n)

@app.get("/api/country/status")
def country_status():
    c = C(); q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE " + w).fetchone()[0]
    return jsonify(job=JOB, with_country=q("country IS NOT NULL AND country<>''"),
                   remaining=q("tmdb_id IS NOT NULL AND (country IS NULL OR country='') AND country_checked IS NULL"),
                   no_tmdb_country=q("tmdb_id IS NOT NULL AND (country IS NULL OR country='') AND country_checked=1"))

@app.post("/api/titles/start")
def titles_start():
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        allf = (request.json or {}).get("all", False) if request.is_json else False
        ids = [(r[0], r[1]) for r in c.execute("SELECT id, tmdb_id FROM films WHERE tmdb_id IS NOT NULL"
               + ("" if allf else " AND alt_titles IS NULL") + " ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="titles", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_titles_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

@app.post("/api/credits/start")
def credits_start():
    """Backfill writers/producers/cinematographer/composer (and director/cast) for films with a tmdb_id."""
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        allf = (request.json or {}).get("all", False)
        ids = [(r[0], r[1]) for r in c.execute("SELECT id, tmdb_id FROM films WHERE tmdb_id IS NOT NULL"
               + ("" if allf else " AND writers IS NULL AND producers IS NULL") + " ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="credits", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_credits_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

@app.post("/api/posters/start")
def posters_start():
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another TMDB job is already running"), 409
        retry = (request.json or {}).get("retry_skipped", False)
        cond = "(poster IS NULL OR poster='') AND tmdb_id IS NULL AND COALESCE(kind,'')<>'scene'" + ("" if retry else " AND COALESCE(tmdb_status,'')<>'needs_match'")
        ids = [r[0] for r in c.execute(f"SELECT id FROM films WHERE {cond} ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="posters", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_poster_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

@app.get("/api/posters/status")
def posters_status(): return jsonify(JOB)

# ---------- resolution detection (local files; independent of the TMDB job so both can run at once) ----------
RES = {"running": False, "kind": "resolution"}
RES_LOCK = threading.Lock()
RES_EXPLICIT = set()  # ids passed explicitly: always re-detect resolution

def run_res_job(ids):
    c = C()
    try:
        for fid in ids:
            if RES["cancel"]: break
            try:
                paths = [r[0] for r in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (fid,))]
                RES["current"] = paths[0] if paths else ""
                st = c.execute("SELECT res_checked, runtime, edited_fields FROM films WHERE id=?", (fid,)).fetchone()
                if paths and st and not st[1] and '"runtime"' not in (st[2] or ""):  # fill empty runtime from the file's real duration
                    rt = resmod.runtime_minutes(paths)
                    if rt and c.execute("UPDATE films SET runtime=? WHERE id=? AND COALESCE(runtime,0)=0 AND COALESCE(edited_fields,'') NOT LIKE '%\"runtime\"%'",
                                        (rt, fid)).rowcount: RES["runtime_filled"] = RES.get("runtime_filled", 0) + 1
                    c.commit()
                if st and st[0] is not None and not RES.get("recheck") and fid not in RES_EXPLICIT:
                    RES["skipped"] += 1; RES["done"] += 1; continue  # resolution already checked: runtime-only pass
                r = resmod.detect(paths) if paths else dict(resolution=None, width=None, height=None, method="none")
                if r is None: RES["offline"] += 1  # drive not mounted: leave unchecked so a later run retries
                else:
                    if r["width"]: c.execute("UPDATE films SET res_width=?, res_height=? WHERE id=?", (r["width"], r["height"], fid))
                    cur = c.execute("UPDATE films SET resolution=? WHERE id=? AND COALESCE(edited_fields,'') NOT LIKE '%\"resolution\"%'",
                                    (r["resolution"], fid)) if r["resolution"] else None
                    if cur is not None and cur.rowcount: RES["matched"] += 1; RES["by_" + r["method"]] = RES.get("by_" + r["method"], 0) + 1
                    else: RES["skipped"] += 1
                    c.execute("UPDATE films SET res_checked=1 WHERE id=?", (fid,))
                c.commit()
            except Exception as e:
                RES["errors"] += 1; RES["error"] = f"{type(e).__name__}: {e}"
            RES["done"] += 1
            el = time.time() - RES["started_at"]
            RES["eta_s"] = int(el / RES["done"] * (RES["total"] - RES["done"])) if RES["done"] else None
    finally:
        RES["running"] = False; RES["finished_at"] = time.time(); RES["current"] = ""; c.close()

def start_res_job(film_ids=None, recheck=False):
    """Start detection for unchecked films (or the given ids). Returns False if one is already running."""
    with RES_LOCK:
        if RES.get("running"): return False
        c = C()
        if film_ids is not None: ids = [int(i) for i in film_ids]
        else:
            ids = [r[0] for r in c.execute("SELECT id FROM films WHERE id IN (SELECT film_id FROM files) AND ((COALESCE(edited_fields,'') NOT LIKE '%\"resolution\"%'"
                   + ("" if recheck else " AND res_checked IS NULL") + ") OR (COALESCE(runtime,0)=0 AND COALESCE(edited_fields,'') NOT LIKE '%\"runtime\"%')) ORDER BY id")]
        c.close()
        RES.clear(); RES.update(running=True, kind="resolution", done=0, total=len(ids), matched=0, skipped=0, offline=0, errors=0,
                                error=None, cancel=False, started_at=time.time(), eta_s=None, current="", method=resmod.method_name(),
                                recheck=bool(recheck), runtime_filled=0)
        RES_EXPLICIT.clear(); RES_EXPLICIT.update(ids if film_ids is not None else ())
        threading.Thread(target=run_res_job, args=(ids,), daemon=True).start()
        return True

@app.post("/api/resolution/start")
def resolution_start():
    """Detect resolution for films not yet checked. {recheck: true} re-checks all (hand-edited values are still kept)."""
    d = (request.json or {}) if request.is_json else {}
    if not start_res_job(d.get("film_ids"), bool(d.get("recheck"))): return jsonify(error="Resolution detection is already running"), 409
    return jsonify(RES)

@app.post("/api/resolution/cancel")
def resolution_cancel(): RES["cancel"] = True; return jsonify(RES)

@app.get("/api/resolution/status")
def resolution_status():
    c = C(); q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE " + w).fetchone()[0]
    return jsonify(job=RES, method=resmod.method_name(), ffprobe=resmod.ffprobe_path() or None,
                   with_resolution=q("resolution IS NOT NULL AND resolution<>''"),
                   remaining=q("res_checked IS NULL AND id IN (SELECT film_id FROM files) AND COALESCE(edited_fields,'') NOT LIKE '%\"resolution\"%'"),
                   unknown=q("res_checked=1 AND COALESCE(resolution,'')=''"),
                   counts=dict(c.execute("SELECT COALESCE(resolution,'(none)'), COUNT(*) FROM films GROUP BY 1").fetchall()))

@app.post("/api/posters/cancel")
def posters_cancel(): JOB["cancel"] = True; return jsonify(JOB)

# ---------- bring-your-own-AI smart tagging (OpenAI-compatible chat completions) ----------
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SMART_TAG_PROMPT.txt")

@app.get("/api/prompt")
def prompt_text():
    try: return Response(open(PROMPT_FILE, encoding="utf-8").read(), mimetype="text/plain")
    except OSError: return Response("", mimetype="text/plain")
AI_BATCH = 8

def ai_endpoint(url):
    u = (url or "").strip().rstrip("/")
    if not u: return None
    if u.endswith("/chat/completions"): return u
    if not _re.search(r"/v\d+[a-z]*$", u): u += "/v1"
    return u + "/chat/completions"

def ai_chat(url, key, model, prompt, timeout=180):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key: headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(ai_endpoint(url), data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r: data = json.load(r)
    return data["choices"][0]["message"]["content"]

def parse_ai_json(text):
    t = _re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (text or "").strip())
    t = _re.sub(r"<think>.*?</think>", "", t, flags=_re.S)  # reasoning models
    try: v = json.loads(t)
    except ValueError:
        i, j = t.find("["), t.rfind("]")
        if i < 0 or j <= i: raise ValueError("AI reply contained no JSON list")
        v = json.loads(t[i:j + 1])
    if isinstance(v, dict): v = v.get("films") or v.get("items") or v.get("results") or [v]
    if not isinstance(v, list): raise ValueError("AI reply was not a JSON list")
    return v

def ai_prompt(films):
    try: base = open(PROMPT_FILE, encoding="utf-8").read()
    except OSError: base = "Suggest tags for each film. Reply ONLY with a JSON list of {film_id, tags:[{name, category}]}.\nFilms:\n"
    slim = [dict(id=f["id"], title=f["title"], year=f["year"], director=f.get("director"), path=f.get("path"),
                 plot=(f.get("plot") or "")[:400] or None, existing_tags=[t["name"] for t in f["existing_tags"]]) for f in films]
    return base + "\n" + json.dumps(slim, ensure_ascii=False, indent=1)

def run_ai_job(cfg, ids):
    c = C(); rl = RateLimiter(2)
    try:
        for i in range(0, len(ids), AI_BATCH):
            if JOB["cancel"]: break
            batch = ids[i:i + AI_BATCH]
            films = [f for f in db.export_queue(c, batch)]
            try:
                rl.wait()
                items = parse_ai_json(ai_chat(cfg["url"], cfg["key"], cfg["model"], ai_prompt(films)))
                valid = {f["id"] for f in films}
                items = [x for x in items if isinstance(x, dict) and x.get("film_id", x.get("id")) in valid]
                r = db.import_ai_tags(c, items)
                got = {x.get("film_id", x.get("id")) for x in items}
                JOB["matched"] += len(got); JOB["skipped"] += len(valid - got); JOB["tags"] = JOB.get("tags", 0) + r["tags_added"]
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:200]
                JOB["errors"] += 1; JOB["error"] = f"AI server error {e.code}: {msg}"
                if e.code in (401, 403, 404): JOB["error"] = f"AI server rejected the request ({e.code}) - check URL, key and model. {msg}"; break
            except (ValueError, KeyError, IndexError, TypeError) as e:
                JOB["errors"] += 1; JOB["error"] = f"Could not read the AI reply: {e}"
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = f"Could not reach the AI server: {e}"
                if JOB["errors"] >= 3 and JOB["matched"] == 0: JOB["error"] += " (stopped)"; break
            JOB["done"] = min(len(ids), i + len(batch))
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

@app.post("/api/ai/start")
def ai_start():
    """{film_ids?:[..]} - tag these films now; omit to process the whole smart-tag queue."""
    c = C(); cfg = dict(url=db.get_setting(c, "ai_url", ""), key=db.get_setting(c, "ai_key", ""), model=db.get_setting(c, "ai_model", ""))
    if not cfg["url"] or not cfg["model"]:
        return jsonify(error="Set up your AI provider first (Library & Settings → Smart tagging with your own AI)"), 400
    ids = (request.json or {}).get("film_ids")
    if not ids: ids = [r[0] for r in c.execute("SELECT film_id FROM smart_queue ORDER BY added_at")]
    if not ids: return jsonify(error="The smart-tag queue is empty"), 400
    with JOB_LOCK:
        if JOB["running"]: return jsonify(error="Another background job is already running"), 409
        JOB.clear(); JOB.update(running=True, kind="ai", done=0, total=len(ids), matched=0, skipped=0, errors=0, tags=0, error=None, cancel=False)
        threading.Thread(target=run_ai_job, args=(cfg, list(ids)), daemon=True).start()
    return jsonify(JOB)

@app.post("/api/ai/test")
def ai_test():
    c = C(); d = request.json or {}
    url = d.get("ai_url") or db.get_setting(c, "ai_url", ""); model = d.get("ai_model") or db.get_setting(c, "ai_model", "")
    key = d.get("ai_key") or db.get_setting(c, "ai_key", "")
    try: return jsonify(ok=True, endpoint=ai_endpoint(url), reply=ai_chat(url, key, model, "Reply with the single word: OK", timeout=60)[:200])
    except urllib.error.HTTPError as e: return jsonify(error=f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}", endpoint=ai_endpoint(url)), 502
    except Exception as e: return jsonify(error=str(e), endpoint=ai_endpoint(url)), 502

@app.post("/api/shutdown")
def shutdown():
    """Stops the (possibly hidden) server. Only reachable from this computer."""
    threading.Timer(0.5, lambda: os._exit(0)).start(); return ok()

@app.get("/api/tmdb/search")
def tmdb_search():
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    p = {"query": request.args["q"]}
    if request.args.get("year"): p["year"] = request.args["year"]
    try: res = tmdb("search/movie", key, **p)["results"][:10]
    except Exception as e: return jsonify(error=str(e)), 502
    return jsonify([dict(id=r["id"], title=r["title"], year=(r.get("release_date") or "")[:4],
                         poster=r.get("poster_path") and "https://image.tmdb.org/t/p/w185" + r["poster_path"],
                         overview=r.get("overview")) for r in res])

def fetch_and_apply(c, fid, tid, keep=(), genres=True):
    """Fetch TMDB movie `tid` and apply it to film `fid`. Returns an error string, or None on success."""
    if is_scene(c, fid): return "Scenes use ThePornDB, not TMDB"
    key = db.get_setting(c, "tmdb_key", "")
    if not key: return "No TMDB API key set in Settings"
    try: m = tmdb(f"movie/{tid}", key, append_to_response="credits,release_dates,alternative_titles", include_adult="true")
    except urllib.error.HTTPError as e:
        return f"TMDB has no movie with id {tid}" if e.code == 404 else "TMDB rejected the API key (401)" if e.code == 401 else f"TMDB error {e.code}"
    except Exception as e: return f"Could not reach TMDB: {e}"
    apply_movie(c, fid, m, respect_edits=True, genres=genres, keep=[k for k in keep if k in FILM_FIELDS])
    c.commit(); return None

@app.post("/api/films/<int:fid>/tmdb")
def tmdb_apply(fid):
    c = C()
    if is_scene(c, fid): return jsonify(error="Scenes use ThePornDB, not TMDB"), 400
    key = db.get_setting(c, "tmdb_key", "")
    if not key: return jsonify(error="No TMDB API key set in Settings"), 400
    d = request.json or {}
    try: tid = parse_tmdb_id(d.get("tmdb_id"))
    except ValueError as e: return jsonify(error=str(e)), 400
    try:
        if not tid:
            f = c.execute("SELECT title, year FROM films WHERE id=?", (fid,)).fetchone()
            p = {"query": f["title"]}
            if f["year"]: p["year"] = f["year"]
            res = tmdb("search/movie", key, **p)["results"]
            if not res: return jsonify(error="No TMDB match"), 404
            tid = res[0]["id"]
        m = tmdb(f"movie/{tid}", key, append_to_response="credits,release_dates,alternative_titles", include_adult="true")
    except urllib.error.HTTPError as e:
        if e.code == 404: return jsonify(error=f"TMDB has no movie with id {tid}"), 404
        if e.code == 401: return jsonify(error="TMDB rejected the API key (401)"), 502
        return jsonify(error=f"TMDB error {e.code}"), 502
    except Exception as e: return jsonify(error=f"Could not reach TMDB: {e}"), 502
    keep = [k for k in d.get("keep", []) if k in FILM_FIELDS]  # fields the user changed in this edit win
    apply_movie(c, fid, m, respect_edits=True, genres=d.get("genres", True), keep=keep)
    c.commit(); return get_film(fid)

# ---------- backup ----------
TABLES = ["films", "files", "categories", "tags", "film_tags", "smart_queue", "aznude_queue", "saved_searches", "settings"]

@app.get("/api/export.json")
def export_json():
    c = C(); data = {t: [dict(r) for r in c.execute(f"SELECT * FROM {t}")] for t in TABLES}
    data["_format"] = "film-tagger-backup-v1"
    return Response(json.dumps(data, indent=1), mimetype="application/json",
                    headers={"Content-Disposition": f"attachment; filename=film-tagger-backup-{time.strftime('%Y%m%d')}.json"})

@app.get("/api/export.csv")
def export_csv():
    c = C(); buf = io.StringIO(); w = csv.writer(buf)
    cols = ["id"] + FILM_FIELDS + ["paths", "tags", "pending_tags"]; w.writerow(cols)
    films = attach(c, [dict(r) for r in c.execute("SELECT * FROM films ORDER BY title COLLATE FOLD")])
    for f in films:
        fmt = lambda ts: "; ".join((t["category"] + ":" if t["category"] else "") + t["name"] for t in ts)
        w.writerow([f["id"]] + [f[k] for k in FILM_FIELDS] + [" | ".join(x["path"] for x in f["files"]), fmt(f["tags"]), fmt(f["pending"])])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=film-tagger-{time.strftime('%Y%m%d')}.csv"})

@app.post("/api/import")
def import_backup():
    """Upload JSON backup (replaces DB) or CSV (merges: rows with matching id updated, else added)."""
    c = C(); f = request.files.get("file"); raw = f.read().decode("utf-8-sig")
    if f.filename.lower().endswith(".json"):
        data = json.loads(raw)
        if data.get("_format") != "film-tagger-backup-v1": return jsonify(error="Not a Film Tagger JSON backup"), 400
        for t in reversed(TABLES): c.execute(f"DELETE FROM {t}")
        for t in TABLES:
            for r in data.get(t, []):
                c.execute(f"INSERT OR REPLACE INTO {t}({','.join(r)}) VALUES({','.join('?'*len(r))})", list(r.values()))
        c.commit(); return ok(mode="replace", films=len(data.get("films", [])))
    n = 0
    for row in csv.DictReader(io.StringIO(raw)):
        vals = clean({k: row.get(k) for k in FILM_FIELDS if k in row})
        fid = int(row["id"]) if row.get("id") and c.execute("SELECT 1 FROM films WHERE id=?", (row["id"],)).fetchone() else None
        if fid: c.execute(f"UPDATE films SET {','.join(k+'=?' for k in vals)} WHERE id=?", list(vals.values()) + [fid])
        else:
            vals.setdefault("title", "Untitled"); vals["added_at"] = time.time()
            fid = c.execute(f"INSERT INTO films({','.join(vals)}) VALUES({','.join('?'*len(vals))})", list(vals.values())).lastrowid
        for p in (row.get("paths") or "").split(" | "):
            if p.strip(): c.execute("INSERT OR IGNORE INTO files(film_id,path) VALUES(?,?)", (fid, p.strip()))
        for t in (row.get("tags") or "").split(";"):
            t = t.strip()
            if t:
                cat, _, name = t.rpartition(":"); db.add_tag(c, fid, name, cat or None, source="import")
        n += 1
    c.commit(); return ok(mode="merge", rows=n)

# ---------- person pages (people.py) ----------
import people as peoplemod
peoplemod.init(app, tmdb, POSTERS_DIR)

# ---------- subtitles (local files; external sidecars + embedded streams, cached by size/mtime) ----------
SUBS = {"running": False, "kind": "subtitles"}
SUBS_LOCK = threading.Lock()

def detect_subs_one(c, fid, dircache=None, force=False):
    """Detect and store one film's subtitles unless hand-overridden (force ignores the override).
    Returns the text, 'manual' if overridden, or None if its drive is not mounted."""
    r = c.execute("SELECT subs_manual FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return None
    if r[0] and not force: return "manual"
    paths = [x[0] for x in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (fid,))]
    v = submod.detect(c, paths, dircache) if paths else submod.NONE
    if v is None: return None
    c.execute("UPDATE films SET subtitles=?, subs_checked=? WHERE id=?" + ("" if force else " AND COALESCE(subs_manual,0)=0"), (v, time.time(), fid))
    c.commit()
    return v

def run_subs_job(ids):
    c = C(); dc = {}
    try:
        for fid in ids:
            if SUBS["cancel"]: break
            try:
                v = detect_subs_one(c, fid, dc)
                if v is None: SUBS["offline"] += 1
                elif v == "manual": SUBS["manual"] += 1
                elif submod.has_subs(v): SUBS["with_subs"] += 1
                else: SUBS["without"] += 1
            except Exception as e:
                SUBS["errors"] += 1; SUBS["error"] = f"{type(e).__name__}: {e}"
            SUBS["done"] += 1
            el = time.time() - SUBS["started_at"]
            SUBS["eta_s"] = int(el / SUBS["done"] * (SUBS["total"] - SUBS["done"])) if SUBS["done"] else None
    finally:
        SUBS["running"] = False; SUBS["finished_at"] = time.time(); c.close()

def start_subs_job(film_ids=None, recheck=False, wait=False):
    with SUBS_LOCK:
        if SUBS.get("running"): return False
        c = C()
        if film_ids is not None: ids = [int(i) for i in film_ids]
        else:
            ids = [r[0] for r in c.execute("SELECT id FROM films WHERE COALESCE(subs_manual,0)=0"
                   + ("" if recheck else " AND subs_checked IS NULL") + " ORDER BY id")]
        c.close()
        SUBS.clear(); SUBS.update(running=True, kind="subtitles", done=0, total=len(ids), with_subs=0, without=0, manual=0, offline=0,
                                  errors=0, error=None, cancel=False, started_at=time.time(), eta_s=None, method=submod.method_name())
        t = threading.Thread(target=run_subs_job, args=(ids,), daemon=True); t.start()
    if wait: t.join()
    return True

def subs_counts(c):
    roots = db.get_setting(c, "roots", []) or []
    rows = c.execute("SELECT f.id, f.subtitles, (SELECT GROUP_CONCAT(path, char(10)) FROM files WHERE film_id=f.id) FROM films f").fetchall()
    tot = dict(films=len(rows), with_subs=0, none=0, unchecked=0); libs = {r.rstrip("\\/"): dict(films=0, with_subs=0, none=0, unchecked=0) for r in roots}
    langs = {}
    for fid, v, ps in rows:
        k = "unchecked" if v is None else ("with_subs" if submod.has_subs(v) else "none")
        tot[k] += 1
        for lg in submod.languages(v): langs[lg] = langs.get(lg, 0) + 1
        pl = [p.lower() for p in (ps or "").split("\n") if p]
        for root, d in libs.items():
            rl = root.lower()
            if any(p.startswith(rl) and len(p) > len(rl) and p[len(rl)] in "\\/" for p in pl): d["films"] += 1; d[k] += 1
    return tot, libs, langs

@app.get("/api/subtitles")
def subtitles_list():
    """Sidebar chip counts: (has), (none) and each language."""
    tot, _, langs = subs_counts(C())
    return jsonify([dict(name="(has)", n=tot["with_subs"]), dict(name="(none)", n=tot["none"] + tot["unchecked"])] +
                   [dict(name=k, n=v) for k, v in sorted(langs.items(), key=lambda kv: (-kv[1], kv[0]))])

@app.post("/api/subtitles/start")
def subtitles_start():
    """Detect subtitles for unchecked films; {recheck:true} for all non-overridden films; {wait:true} blocks until done."""
    d = (request.json or {}) if request.is_json else {}
    if not start_subs_job(d.get("film_ids"), bool(d.get("recheck")), bool(d.get("wait"))): return jsonify(error="Subtitle detection is already running"), 409
    return jsonify(SUBS)

@app.post("/api/subtitles/cancel")
def subtitles_cancel(): SUBS["cancel"] = True; return jsonify(SUBS)

@app.get("/api/subtitles/status")
def subtitles_status():
    tot, libs, langs = subs_counts(C())
    return jsonify(job=SUBS, method=submod.method_name(), ffprobe=resmod.ffprobe_path() or None, totals=tot, libraries=libs, languages=langs)

@app.post("/api/films/<int:fid>/subtitles/redetect")
def subtitles_redetect(fid):
    """Clear the manual override and detect this film's subtitles now."""
    c = C()
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
    ed = set(json.loads(r[0] or "[]")); ed.discard("subtitles")
    c.execute("UPDATE films SET subs_manual=0, edited_fields=? WHERE id=?", (json.dumps(sorted(ed)), fid)); c.commit()
    v = detect_subs_one(c, fid, force=True)
    j = get_film(fid).get_json(); j["subs_offline"] = v is None
    return jsonify(j)


# ---------- scenes (ThePornDB; TMDB is never used for kind='scene') ----------
def is_scene(c, fid):
    r = c.execute("SELECT kind FROM films WHERE id=?", (fid,)).fetchone()
    return bool(r) and r[0] == "scene"

def keep_parent_studio(c, fid, s, new):
    """True if the film already has a studio and ThePornDB's studio is a sub-label/parent of it (Kyle's link, or the
    site's parent/network on ThePornDB), so the film keeps its own studio (e.g. folder "Devil's Film")."""
    cur = (c.execute("SELECT COALESCE(studio,'') FROM films WHERE id=?", (fid,)).fetchone() or [""])[0].strip()
    if not cur or db.studio_key(cur) == db.studio_key(new): return False
    ck = {db.studio_key(n) for n in db.studio_names(cur, c)}
    if db.studio_key(new) in {db.studio_key(x) for x in db.related_studios(cur, c)}: return True
    site = (s or {}).get("site") or {}
    fam = [site.get(k, {}).get("name") for k in ("parent", "network") if isinstance(site.get(k), dict)]
    return any(db.studio_key(x) in ck for x in fam if x)

def apply_scene(c, fid, s, keep=()):
    """Apply a ThePornDB scene to a film. Hand-edited fields (edited_fields) and `keep` are never replaced; tags are only added."""
    f = tpdb.scene_fields(s)
    ed = set(json.loads((c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or "[]")) | set(keep)
    upd = {k: v for k, v in f.items() if k != "tags" and v not in (None, "") and k not in ed}
    if is_review(c, fid): upd.pop("poster", None); upd.pop("poster_wide", None)  # possibly-underage review flag: never fetch images
    if upd.get("studio"): upd["studio"] = db.canon_studio(upd["studio"], c)  # 'angels.love' -> 'Angels Love'
    if upd.get("studio") and keep_parent_studio(c, fid, s, upd["studio"]): upd.pop("studio")  # never swap Kyle's studio for a sub-label
    upd.update(kind="scene", tpdb_checked=time.time(), updated_at=time.time())
    c.execute(f"UPDATE films SET {','.join(k + '=?' for k in upd)} WHERE id=?", list(upd.values()) + [fid])
    for t in f["tags"]: db.add_tag_if_absent(c, fid, t, "Content", source="tpdb")
    db.set_primary_genre(c, fid, "Hardcore", only_if_empty=True)
    if "poster" in upd or "poster_wide" in upd: kick_focus()  # new cover: smart crop runs in the background once committed

def is_review(c, fid):
    r = c.execute("SELECT COALESCE(notes,'') FROM films WHERE id=?", (fid,)).fetchone()
    return bool(r) and "Review by hand" in r[0]

TPC = {}  # candidate cache: (film id, query) -> result
@app.get("/api/films/<int:fid>/tpdb/candidates")
def tpdb_candidates(fid):
    """Top ThePornDB candidates for a scene from its cleaned file name, or ?q= typed text. No images for review-flagged scenes."""
    c = C(); token = db.get_setting(c, "tpdb_key", "")
    if not token: return jsonify(error="No ThePornDB API token set in Settings"), 400
    if not is_scene(c, fid): return jsonify(error="Not a scene"), 400
    q = (request.args.get("q") or "").strip() or None
    p = c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id LIMIT 1", (fid,)).fetchone()
    f = c.execute("SELECT studio, release_date, cast_, title, COALESCE(tpdb_id,'') tid FROM films WHERE id=?", (fid,)).fetchone()
    cur_id = f["tid"].strip()
    src = p[0] if p else ""
    if not q and cur_id:  # already matched: search from the DB fields, with the studio given as the folder (like a fresh file)
        src = scene_query(f)
    key = (fid, q, cur_id, src)
    if key not in TPC or request.args.get("refresh"):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(2) as ex:  # current match by id and the candidate search run in parallel
            fc = ex.submit(tpdb.get_scene, token, cur_id) if cur_id else None
            fs = ex.submit(tpdb.candidates, token, src, query=q, limit=int(request.args.get("limit", 8)))
            try: r = fs.result()
            except Exception as e: return jsonify(error=tpdb_err(e)), 502
            cur = None
            if fc:
                try: cur = fc.result() or None
                except Exception: cur = None
        TPC[key] = dict(r, current=cur)
    r = TPC[key]; img = not is_review(c, fid)
    res = [dict(tpdb.brief(e["scene"], img), score=e["score"], why=e["why"], confident=e["confident"]) for e in r["results"]]
    if cur_id:
        res = [x for x in res if str(x.get("id")) != cur_id]
        if r.get("current"): res.insert(0, dict(tpdb.brief(r["current"], img), score="", why=["current match"], confident=False, current=True))
        else: res.insert(0, dict(id=cur_id, title="(current match could not be loaded from ThePornDB)", performers=[], current=True, score="", why=[], confident=False))
    return jsonify(query=r["query"], review=not img, file=os.path.basename(p[0]) if p else None, matched=bool(cur_id), results=res)

def scene_query(f):
    """Pseudo path for a matched scene from its DB fields: '<Studio>/<YYYY-MM-DD> <performers> <title>.mp4'
    (candidates() takes the studio from the folder, so names like 'Cum 4K' survive the file-name cleaning)."""
    perf = " ".join(split_list(f["cast_"] or "")[:3])
    stem = " ".join(x for x in [f["release_date"] or "", perf, (f["title"] or "").replace("/", " ")] if x).strip()
    return f"{(f['studio'] or '').replace('/', ' ')}/{stem}.mp4" if f["studio"] else f"{stem}.mp4"

@app.get("/api/tpdb/unmatched")
def tpdb_unmatched():
    """Scenes without a ThePornDB id (for the Review unmatched view)."""
    c = C()
    rows = c.execute("SELECT f.id, f.title, f.studio, COALESCE(f.notes,'') LIKE '%Review by hand%' review, "
                     "(SELECT path FROM files x WHERE x.film_id=f.id ORDER BY id LIMIT 1) path FROM films f "
                     "WHERE f.kind='scene' AND COALESCE(f.tpdb_id,'')='' ORDER BY f.studio COLLATE FOLD, f.title COLLATE FOLD").fetchall()
    return jsonify(scenes=[dict(id=r["id"], title=r["title"], studio=r["studio"], review=bool(r["review"]), file=os.path.basename(r["path"] or "")) for r in rows])

def tpdb_err(e):
    if isinstance(e, urllib.error.HTTPError):
        return {401: "ThePornDB rejected the API token (401)", 404: "ThePornDB has no such scene"}.get(e.code, f"ThePornDB error {e.code}")
    return f"Could not reach ThePornDB: {e}"

@app.post("/api/films/<int:fid>/tpdb")
def tpdb_apply(fid):
    """{tpdb_id: id/URL or blank (= look up from the filename), keep: [fields changed in the edit window]}"""
    c = C(); token = db.get_setting(c, "tpdb_key", "")
    if not token: return jsonify(error="No ThePornDB API token set in Settings (free at theporndb.net)"), 400
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    d = request.json or {}
    try: sid = tpdb.parse_id(d.get("tpdb_id") or "")
    except ValueError as e: return jsonify(error=str(e)), 400
    try:
        if sid: s = tpdb.get_scene(token, sid)
        else:
            p = c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id LIMIT 1", (fid,)).fetchone()
            s = tpdb.lookup(token, p[0]) if p else None
            if s: s = tpdb.get_scene(token, s.get("id") or s.get("_id")) or s
        if not s: return jsonify(error="No ThePornDB match (no confident match; use Find… to pick a candidate)"), 404
    except Exception as e: return jsonify(error=tpdb_err(e)), 502
    apply_scene(c, fid, s, keep=[k for k in d.get("keep", []) if k in FILM_FIELDS]); c.commit()
    if d.get("rename", True):
        try: rename_scene_file(c, fid)
        except OSError as e: return jsonify(error=f"Matched, but the file could not be renamed: {e}"), 500
    return get_film(fid)

def _safe(s, n=80):
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip().rstrip(". ")
    return s[:n].rstrip(". ") if len(s) > n else s

def scene_file_name(f):
    """'<Studio> - YYYY-MM-DD - <Performers> - <Title>' (same scheme as the scene import)."""
    perf = [x.strip() for x in (f["cast_"] or "").split(",") if x.strip()]
    ps = ", ".join(perf[:4]) + (" et al" if len(perf) > 4 else "")
    return " - ".join(x for x in [_safe(f["studio"], 60), f["release_date"] or "", _safe(ps, 90), _safe(f["title"], 90)] if x)

SCENE_NAME_FIELDS = ("studio", "release_date", "cast_", "title")
KEEP_DIRS = {"_no studio"}
HOLD_RENAME = "[Hold rename]"  # in notes: Fix all file names skips the scene (shaky match / raw-filename metadata)

def rename_held(c, fid):
    r = c.execute("SELECT COALESCE(notes,'') FROM films WHERE id=?", (fid,)).fetchone()
    return bool(r) and HOLD_RENAME.lower() in r[0].lower()

def plan_scene_rename(c, fid):
    """Planned moves for one scene: ([dict(file_id, old, new, root, sidecars=[(a, b)])], note or None).
    Target: <scenes root>/<Studio>/<Studio> - YYYY-MM-DD - <Performers> - <Title>.<ext>; never overwrites (' (2)')."""
    f = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return [], "not found"
    miss = [n for k, n in (("studio", "studio"), ("release_date", "date"), ("title", "title")) if not (f[k] or "").strip()]
    if miss: return [], "Not renamed: missing " + ", ".join(miss)
    out, taken = [], set()
    for x in c.execute("SELECT id, path FROM files WHERE film_id=? ORDER BY id", (fid,)).fetchall():
        old = x["path"]; root = next((r for r in db.scene_roots(c) if db.under(old, r)), None)
        if not root or not os.path.exists(old): continue
        ext = os.path.splitext(old)[1].lower(); folder = os.path.join(root, _safe(f["studio"], 60))
        base = os.path.join(folder, scene_file_name(f)); new = base + ext; n = 2
        while (os.path.exists(new) and os.path.normcase(new) != os.path.normcase(old)) or os.path.normcase(new) in taken:
            new = f"{base} ({n}){ext}"; n += 1
        taken.add(os.path.normcase(new))
        gk, gd = gallerymod.find(old, db.VIDEO_EXT)  # the photo gallery moves with the video (legacy 'gallery' -> canonical)
        if not db.same_drive(old, new): continue  # never plan a cross-drive move
        gal = None
        if gd:
            gnew = gallerymod.canonical_dir(new)
            if os.path.normcase(gnew) != os.path.normcase(gd) and not os.path.exists(gnew): gal = (gd, gnew)
        if new == old and not gal: continue
        st, dd = os.path.splitext(os.path.basename(old))[0], os.path.dirname(old)
        side = [(os.path.join(dd, y), os.path.splitext(new)[0] + y[len(st):]) for y in os.listdir(dd)
                if y.lower().startswith(st.lower() + ".") and os.path.normcase(os.path.join(dd, y)) != os.path.normcase(old)
                and not os.path.isdir(os.path.join(dd, y))]
        if new == old: side = []
        out.append(dict(file_id=x["id"], old=old, new=new, root=root, sidecars=side, gallery=gal))
    return out, None

def do_scene_moves(c, moves):
    """Execute planned moves: rename file + sidecars, update the files table (path index) and subtitle cache, log CSV,
    drop the old folder if now empty (never the root or '_No Studio')."""
    import csv
    done = 0
    for m in moves:
        old, new = m["old"], m["new"]
        if os.path.exists(new) and os.path.normcase(new) != os.path.normcase(old): continue  # appeared since planning
        g0 = m.get("gallery")
        if not db.same_drive(old, new) or any(not db.same_drive(a, b) for a, b in m["sidecars"]) or (g0 and not db.same_drive(*g0)):
            continue  # guard: refuse any cross-drive move
        os.makedirs(os.path.dirname(new), exist_ok=True)
        pairs = ([(old, new)] if new != old else []) + [(a, b) for a, b in m["sidecars"] if os.path.exists(a) and not os.path.exists(b)]
        g = m.get("gallery")
        if g and os.path.isdir(g[0]) and not os.path.exists(g[1]): pairs.append(g)
        for a, b in pairs:
            if os.path.isdir(a):
                import shutil; shutil.move(a, b)
            else: os.rename(a, b)
        if g: c.execute("UPDATE films SET gallery_path=? WHERE id=(SELECT film_id FROM files WHERE id=?)", (g[1], m["file_id"]))
        c.execute("UPDATE files SET path=?, missing=0 WHERE id=?", (new, m["file_id"]))
        c.execute("UPDATE OR IGNORE sub_probe SET path=? WHERE path=?", (new, old)); c.commit(); done += 1
        with open(os.path.join(m["root"], "_filmtagger_renames.csv"), "a", newline="", encoding="utf-8-sig") as fh: csv.writer(fh).writerows(pairs)
        dd = os.path.dirname(old)
        try:
            if (not os.listdir(dd) and os.path.normcase(dd) != os.path.normcase(m["root"])
                    and os.path.basename(dd).lower() not in KEEP_DIRS): os.rmdir(dd)
        except OSError: pass
    return done

def rename_scene_file(c, fid):
    """Move a scene's file(s) (and same-name sidecars) to the standard name. Returns (renamed count, note)."""
    moves, note = plan_scene_rename(c, fid)
    return (do_scene_moves(c, moves) if moves else 0), note

@app.get("/api/scenes/rename-preview")
def scenes_rename_preview():
    """Every scene whose file name differs from the standard name: [{id, old, new}] (nothing is moved)."""
    c = C(); items, skipped, held = [], [], 0
    for (fid,) in c.execute("SELECT id FROM films WHERE kind='scene' ORDER BY studio COLLATE FOLD, title COLLATE FOLD").fetchall():
        if rename_held(c, fid): held += 1; continue
        moves, note = plan_scene_rename(c, fid)
        if note: skipped.append(dict(id=fid, note=note)); continue
        items += [dict(id=fid, old=m["old"], new=m["new"], gallery_old=m["gallery"][0] if m.get("gallery") else None,
                       gallery_new=m["gallery"][1] if m.get("gallery") else None) for m in moves]
    gal = [dict(id=x["id"], old=x["gallery_old"], new=x["gallery_new"]) for x in items if x["gallery_old"]]
    return jsonify(count=len(items), items=items, skipped=len(skipped), held=held, galleries=gal)

@app.post("/api/scenes/rename-apply")
def scenes_rename_apply():
    """Apply the standard names to all scenes (or {ids:[...]}). Same logging as Rename on Save."""
    c = C(); ids = (request.json or {}).get("ids")
    if not ids: ids = [r[0] for r in c.execute("SELECT id FROM films WHERE kind='scene'")]
    n, errors = 0, []
    for fid in ids:
        if rename_held(c, int(fid)): continue
        try: n += rename_scene_file(c, int(fid))[0]
        except OSError as e: errors.append(dict(id=fid, error=str(e)))
    return jsonify(renamed=n, errors=errors)

# ---------- scene photo galleries (gallery.py): listing, cached thumbnails, full images, use as poster ----------
GALLERY_THUMBS = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "gallery_thumbs")

def scene_gallery(c, fid):
    """-> (kind, folder) for a scene's first file with a gallery; updates films.gallery_path/count if they changed."""
    kind = folder = None
    for (p,) in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (fid,)).fetchall():
        kind, folder = gallerymod.find(p, db.VIDEO_EXT)
        if folder: break
    n = len(gallerymod.images(folder)) if folder else 0
    r = c.execute("SELECT gallery_path, gallery_count FROM films WHERE id=?", (fid,)).fetchone()
    if r and (r[0] != folder or (r[1] or 0) != n):
        c.execute("UPDATE films SET gallery_path=?, gallery_count=? WHERE id=?", (folder, n or None, fid)); c.commit()
    return kind, folder

def refresh_galleries(c=None):
    """Re-detect galleries for every scene (startup, after scans). Returns {film_id: count}."""
    c = c or C(); out = {}
    for (fid,) in c.execute("SELECT id FROM films WHERE kind='scene'").fetchall():
        k, f = scene_gallery(c, fid)
        if f: out[fid] = len(gallerymod.images(f))
    return out

def _gallery_or_404(fid):
    c = C()
    if is_review(c, fid): return None, (jsonify(error="Gallery hidden: this scene is flagged for review by hand"), 403)
    kind, folder = scene_gallery(c, fid)
    if not folder: return None, (jsonify(error="No gallery for this scene"), 404)
    return (c, kind, folder), None

@app.get("/api/films/<int:fid>/gallery")
def gallery_list(fid):
    g, err = _gallery_or_404(fid)
    if err: return err
    c, kind, folder = g; names = gallerymod.images(folder)
    return jsonify(kind=kind, folder=os.path.basename(folder), count=len(names), images=names)

@app.get("/api/films/<int:fid>/gallery/thumb")
def gallery_thumb(fid):
    g, err = _gallery_or_404(fid)
    if err: return err
    p = gallerymod.safe_name(g[2], request.args.get("name"))
    if not p: return jsonify(error="not found"), 404
    t = gallerymod.thumb(p, GALLERY_THUMBS)
    return send_file(t or p, max_age=86400 * 30)

@app.get("/api/films/<int:fid>/gallery/image")
def gallery_image(fid):
    g, err = _gallery_or_404(fid)
    if err: return err
    p = gallerymod.safe_name(g[2], request.args.get("name"))
    if not p: return jsonify(error="not found"), 404
    return send_file(p, max_age=86400)

@app.post("/api/films/<int:fid>/gallery/poster")
def gallery_poster(fid):
    """{name}: copy that gallery photo into the posters folder and use it as the custom poster (smart crop runs after)."""
    g, err = _gallery_or_404(fid)
    if err: return err
    c = g[0]; p = gallerymod.safe_name(g[2], (request.json or {}).get("name"))
    if not p: return jsonify(error="not found"), 404
    if os.path.getsize(p) > POSTER_SRC_MAX: return jsonify(error="Image is too large (max 200 MB)"), 413
    with open(p, "rb") as fh: data = fh.read()  # read only; the gallery file is never changed
    ext = sniff_image(data)
    if not ext: return jsonify(error="That file is not a JPG, PNG or WebP image"), 415
    if len(data) > POSTER_MAX:  # big camera photos: save a downscaled JPEG copy as the poster
        data, ext = fit_poster(data)
        if data is None: return jsonify(error=ext), 413
    val = _save_poster(c, fid, data, ext)
    c.execute("UPDATE films SET custom_poster=?, updated_at=? WHERE id=?", (val, time.time(), fid)); c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note="from gallery"))

# ---------- Find Gallery: guest ViperGirls search + gallery-dl download (findgallery.py) ----------
import findgallery as fgmod

def _fg_scene(fid):
    """-> (c, film row, first video path) or an error response. Review-flagged scenes are blocked."""
    c = C(); f = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not f or f["kind"] != "scene": return None, (jsonify(error="Not a scene"), 404)
    if is_review(c, fid): return None, (jsonify(error="Blocked: this scene is flagged for review by hand"), 403)
    p = c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id LIMIT 1", (fid,)).fetchone()
    return (c, f, p[0] if p else None), None

FG_KNOWN = {}  # fold(name) -> bool: ThePornDB performer lookups for unmatched scenes (this session)

def fg_known_name(c, name):
    """True if `name` is a known performer: in people (name or alias), in the cast of a ThePornDB-matched scene,
    or an exact ThePornDB performer (name or alias). Used so title words never become search keywords."""
    k = db.fold(" ".join(name.split()))
    if len(k.split()) < 2: return False
    if k in db.alias_groups(c): return True
    try:
        if c.execute("SELECT 1 FROM people WHERE name_norm=? LIMIT 1", (k,)).fetchone(): return True
    except Exception: pass
    if c.execute("SELECT 1 FROM films WHERE COALESCE(tpdb_id,'')<>'' AND person_in_any(cast_, ?) LIMIT 1", (" ".join(name.split()),)).fetchone(): return True
    if k not in FG_KNOWN:
        FG_KNOWN[k] = False
        token = db.get_setting(c, "tpdb_key", "")
        if token:
            try: FG_KNOWN[k] = bool(peoplemod.pick_performer(tpdb.call("/performers", token, q=name).get("data") or [], name))
            except Exception: FG_KNOWN.pop(k, None)
    return FG_KNOWN.get(k, False)

def _fg_parts(f, c=None):
    """Performer names to search with (never title words) and the studio's names.
    Matched scenes (tpdb_id) trust their cast. Otherwise each cast entry must be a known performer; a run-on cast
    ('Cum4K Chloe Rose Come Fuck Me') keeps only its leading word pairs that are known performers ('Chloe Rose')."""
    studios = db.studio_names(f["studio"]) if f["studio"] else []
    cast = split_list(f["cast_"] or "")
    try: matched = bool((f["tpdb_id"] or "").strip())
    except (IndexError, KeyError): matched = False
    if matched or c is None: return cast, studios
    out = []
    for entry in cast:
        if len(entry.split()) <= 3 and fg_known_name(c, entry): out.append(entry); continue
        words = fgmod.guess_names(entry, studios)  # studio words and numbers dropped
        w = " ".join(words).split()
        i = 0
        while i + 1 < len(w) and w[i][:1].isupper() and w[i + 1][:1].isupper():
            pair = f"{w[i]} {w[i + 1]}"
            if not fg_known_name(c, pair): break
            out.append(pair); i += 2
    return list(dict.fromkeys(out)), studios

FG_ALIAS = {}  # fold(name) -> (aliases, source): ThePornDB lookups made by Find Gallery this session

def fg_aliases(c, name):
    """Multi-word aliases of a performer for Find Gallery: people.aliases (and merges) from the DB; if none, ThePornDB's
    performer aliases, cached into people.aliases add-only (nothing Kyle set is removed). -> (aliases, 'db'|'tpdb'|None)."""
    k = db.fold(" ".join(name.split()))
    c.executescript(peoplemod.PEOPLE_SCHEMA); peoplemod.migrate(c)
    al = [a for a in db.expand_person(c, name)[1:] if db._alias_ok(a)]
    if al: return al, "db"
    if k in FG_ALIAS: return FG_ALIAS[k]
    out = ([], None)
    token = db.get_setting(c, "tpdb_key", "")
    if token:
        try:
            hit = peoplemod.pick_performer(tpdb.call("/performers", token, q=name).get("data") or [], name)
            tal = [" ".join(a.split()) for a in (hit or {}).get("aliases") or [] if isinstance(a, str) and a.strip()]
            if tal:
                row = c.execute("SELECT id, aliases FROM people WHERE name_norm=?", (k,)).fetchone()
                try: cur = json.loads(row["aliases"] or "[]") if row else []
                except ValueError: cur = []
                have = {db.fold(x) for x in cur}
                merged = cur + [a for a in tal if db.fold(a) not in have and db.fold(a) != k]
                if row: c.execute("UPDATE people SET aliases=? WHERE id=?", (json.dumps(merged), row["id"]))
                else: c.execute("INSERT INTO people(name, name_norm, aliases) VALUES(?,?,?)", (" ".join(name.split()), k, json.dumps(merged)))
                c.commit()
                out = ([a for a in tal if db._alias_ok(a) and db.fold(a) != k], "tpdb")
        except Exception:
            pass
    FG_ALIAS[k] = out
    return out

@app.post("/api/films/<int:fid>/findgallery/search")
def findgallery_search(fid):
    """{query?: str, prefix?: bool}. Default: performers + the studio's prefix tag; if nothing, performers + studio as text."""
    g, err = _fg_scene(fid)
    if err: return err
    c, f, _ = g; d = request.json or {}
    perfs, studios = _fg_parts(f, c)
    default_q = " ".join(perfs + studios[:1]).strip()
    try: plist = fgmod.prefixes(os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "vg_prefixes.json"))
    except Exception: plist = []
    old_prefix = fgmod.match_prefix(studios, plist) if studios else None
    rule = db.vg_learned_get(c, studios) if studios else None  # learned from a thread URL Kyle pasted by hand
    prefix = (rule or {}).get("prefix") or old_prefix  # learned prefix first, then the old one
    alt_prefix = old_prefix if old_prefix and old_prefix != prefix else None
    no_names = bool(rule) and rule.get("names_in_title") == 0  # this studio's thread titles leave performer names out
    q = (d.get("query") or "").strip(); tried = []
    alias_map, alias_src = {}, {}
    for one in perfs[:3]:
        alias_map[one], alias_src[one] = fg_aliases(c, one) if not q else ([], None)
    # planned steps (at most fgmod.MAX_STEPS forum searches): each (query, prefix); stop at the first with results
    if q:
        steps = [(q, prefix if d.get("prefix", True) else None)]
    else:
        if not perfs: return jsonify(error="No known performers for this scene: add performers or type a name", default_query="", hint="add performers or type a name"), 400
        # Thread titles often leave out co-stars (e.g. the male performer), and vBulletin ANDs every keyword, so search
        # one performer at a time (first billed first), then their aliases. Last step: performers + studio, no prefix.
        # Every step stays in Adult Photo Sets (+ subforums); there is no all-forums search.
        head = [(one, prefix) for one in perfs[:2]] if prefix else []
        lead_al = [(a, prefix) for a in alias_map[perfs[0]]] if prefix else []
        co_al = [(a, prefix) for one in perfs[1:2] for a in alias_map[one]] if prefix else []
        plain = [(default_q, None)]
        # last resort: the lead performer's last name (and each alias's last word) alone, only with a studio prefix
        lasts = []
        if prefix:
            for n in [perfs[0]] + alias_map[perfs[0]]:
                w = n.split()[-1] if len(n.split()) > 1 else ""
                if len(w) >= 3 and db.fold(w) not in {db.fold(x) for x in lasts}: lasts.append(w)
        last = [(w, prefix, False, "last") for w in lasts]
        alt = [(one, alt_prefix) for one in perfs[:1]] if alt_prefix else []  # the old prefix, after the learned one
        room = fgmod.MAX_STEPS - len(head) - len(alt) - len(plain) - len(last)
        mid = (lead_al + co_al)[:max(0, room)]  # co-star aliases are trimmed first, then the lead's
        # learned: titles leave names out -> prefix + last names right after the full names, before aliases
        steps = head + last + mid + alt + plain if no_names else head + mid + alt + plain + last
        steps = steps[:fgmod.MAX_STEPS]
    steps = [tuple(x) + (False, "")[len(x) - 2:] for x in steps]
    steps = list(dict.fromkeys(steps))[:fgmod.MAX_STEPS]
    res, pref_used, t0, last_name = [], False, time.time(), False
    fgmod.SEARCH.update(started=t0, n=len(steps), step="")
    try:
        for k, (sq, sp, wide, kind) in enumerate(steps):
            if k and time.time() - t0 > fgmod.SEARCH_BUDGET_S:
                tried.append(dict(query=sq, prefix=sp, n=0, skipped="time limit")); break
            fgmod.SEARCH["step"] = f"Step {k + 1} of {len(steps)}: “{sq}”" + (f" with prefix {sp}" if sp else "") + (" in all forums" if wide else "")
            res, cached = fgmod.search(sq, sp, block=k > 0, all_forums=wide)
            tried.append(dict(query=sq, prefix=sp, n=len(res), cached=cached, all_forums=wide, last_name=kind == "last")); pref_used = bool(sp)
            last_name = kind == "last"
            if res: break
    except fgmod.Busy as b:
        return jsonify(error=f"Another search ran just now; try again in {b.seconds:.0f} s", wait=round(b.seconds), default_query=default_q, tried=tried), 429
    except Exception as e:
        msg = "the forum did not answer in time" if "timed out" in str(e).lower() or isinstance(e, TimeoutError) else type(e).__name__
        return jsonify(error=f"Search failed: {msg}", default_query=default_q, tried=tried), 502
    finally:
        fgmod.SEARCH.update(step="", started=0.0)
    names = [[p] + (alias_map.get(p) or fg_aliases(c, p)[0]) for p in perfs]
    res = fgmod.rank([dict(r) for r in res], names, studios, f["release_date"], pref_used)
    if last_name:  # found only by a last name: labelled, and ranked below any full-name match
        for r in res: r.update(last_name_match=True, score=round(r["score"] - 1, 2))
    learned = dict(studio=rule["studio"], prefix=rule.get("prefix"), prefix_label=rule.get("prefix_label"),
                   names_in_title=rule.get("names_in_title"), notes=rule.get("notes")) if rule else None
    return jsonify(default_query=default_q, prefix=prefix, tried=tried, results=res[:60], learned=learned,
                   aliases={p: dict(names=alias_map.get(p) or [], source=alias_src.get(p)) for p in perfs[:3]})

@app.post("/api/films/<int:fid>/findgallery/download")
def findgallery_download(fid):
    """{url, overwrite?}: run gallery-dl into '<video stem> - GALLERY' beside the video (one job at a time)."""
    g, err = _fg_scene(fid)
    if err: return err
    c, f, path = g; d = request.json or {}
    if not path: return jsonify(error="This scene has no video file"), 400
    url = fgmod.normalize_url(d.get("url"))
    if not url: return jsonify(error="Paste a vipergirls.to thread link (https://vipergirls.to/threads/…); bare /posts/ links can't be downloaded"), 400
    learned = fg_learn(c, f, url) if d.get("manual") else None  # only from URLs Kyle pastes by hand
    if fgmod.DL.get("running"): return jsonify(error="A gallery download is already running", learned=learned), 409
    kind, cur = gallerymod.find(path, db.VIDEO_EXT)
    dest = cur if kind == "canonical" else gallerymod.canonical_dir(path)  # an existing (even long-named) gallery is kept
    existing = len(gallerymod.images(cur)) if cur else 0
    if existing and not d.get("overwrite"):
        return jsonify(error=f"This scene already has a gallery with {existing} photos", confirm=True, existing=existing, learned=learned), 409
    def done(): scene_gallery(C(), fid)
    fgmod.DL.update(running=True, film_id=fid, files=0, error=None)
    threading.Thread(target=fgmod.run_download, args=(url, dest, done), daemon=True).start()
    return jsonify(ok=True, url=url, dest=os.path.basename(dest), learned=learned)

def fg_learn(c, f, url):
    """Learn this studio's ViperGirls rule (prefix used, whether titles name performers) from a hand-pasted thread:
    one guest page read of the thread. A hand-pasted URL always replaces the stored rule. -> the rule or None."""
    if not f["studio"]: return None
    tu = fgmod.thread_url(url)
    if not tu: return None
    try:
        perfs, studios = _fg_parts(f, c)
        try: plist = fgmod.prefixes(os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "vg_prefixes.json"))
        except Exception: plist = []
        names = [[p] + fg_aliases(c, p)[0] for p in perfs]
        r = fgmod.learn(fgmod.http(tu), db.canon_studio(f["studio"], c) or f["studio"], names, plist)
        if not r: return None
        db.vg_learned_set(c, db.canon_studio(f["studio"], c) or f["studio"], r["prefix"], r["prefix_label"], r["names_in_title"], r["notes"], tu)
        return r
    except Exception as e:
        fgmod.log.warning("Find Gallery: could not learn from %s: %s", tu, type(e).__name__)
        return None

@app.get("/api/vg-learned")
def vg_learned_list():
    return jsonify(rules=db.vg_learned_list(C()))

@app.delete("/api/vg-learned")
def vg_learned_remove():
    k = (request.args.get("key") or "").strip()
    if not k: return jsonify(error="key required"), 400
    return jsonify(ok=True, removed=db.vg_learned_delete(C(), k))

@app.get("/api/findgallery/status")
def findgallery_status():
    j = {k: v for k, v in fgmod.DL.items() if k in ("running", "film_id", "url", "files", "error", "removed", "rc", "started_at", "finished_at", "last_msg")}
    j["dest"] = os.path.basename(fgmod.DL.get("dest") or "")
    st = fgmod.SEARCH
    j["search"] = dict(step=st["step"], secs=round(time.time() - st["started"]) if st["started"] else 0) if st["step"] else None
    return jsonify(j)

@app.post("/api/findgallery/cancel")
def findgallery_cancel():
    fgmod.DL["cancel"] = True; return jsonify(ok=True)

@app.post("/api/galleries/refresh")
def galleries_refresh():
    found = refresh_galleries()
    return jsonify(scenes=len(found), photos=sum(found.values()))

threading.Thread(target=lambda: (time.sleep(3), refresh_galleries()), daemon=True).start()  # detect galleries in the background at startup

TP = {"running": False, "kind": "tpdb"}
TP_LOCK = threading.Lock()

def run_tpdb_job(token, ids):
    c = C(); rl = RateLimiter(2)
    try:
        for fid in ids:
            if TP["cancel"]: break
            try:
                p = c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id LIMIT 1", (fid,)).fetchone()
                rl.wait(); s = tpdb.lookup(token, p[0]) if p else None
                if s:
                    rl.wait(); s = tpdb.get_scene(token, s.get("id") or s.get("_id")) or s
                    apply_scene(c, fid, s); TP["matched"] += 1
                else:
                    c.execute("UPDATE films SET tpdb_checked=? WHERE id=?", (time.time(), fid)); TP["skipped"] += 1
                c.commit()
            except urllib.error.HTTPError as e:
                TP["errors"] += 1; TP["error"] = tpdb_err(e)
                if e.code == 401: break
            except Exception as e:
                TP["errors"] += 1; TP["error"] = tpdb_err(e)
            TP["done"] += 1
    finally:
        TP["running"] = False; TP["finished_at"] = time.time(); c.close()

def start_tpdb_job(film_ids=None, retry=False, wait=False):
    """Look up scenes without a ThePornDB id. No token -> does nothing (returns None)."""
    c = C(); token = db.get_setting(c, "tpdb_key", "")
    if not token: c.close(); return None
    with TP_LOCK:
        if TP.get("running"): c.close(); return False
        ids = [int(i) for i in film_ids] if film_ids is not None else [r[0] for r in c.execute(
            "SELECT id FROM films WHERE kind='scene' AND tpdb_id IS NULL" + ("" if retry else " AND tpdb_checked IS NULL") + " ORDER BY id")]
        c.close()
        TP.clear(); TP.update(running=True, kind="tpdb", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        t = threading.Thread(target=run_tpdb_job, args=(token, ids), daemon=True); t.start()
    if wait: t.join()
    return True

@app.post("/api/tpdb/start")
def tpdb_start():
    d = (request.json or {}) if request.is_json else {}
    r = start_tpdb_job(d.get("film_ids"), bool(d.get("retry")), bool(d.get("wait")))
    if r is None: return jsonify(error="No ThePornDB API token set in Settings (free at theporndb.net)"), 400
    if r is False: return jsonify(error="ThePornDB lookup is already running"), 409
    return jsonify(TP)

@app.get("/api/tpdb/status")
def tpdb_status():
    c = C(); q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE " + w).fetchone()[0]
    return jsonify(job=TP, token_set=bool(db.get_setting(c, "tpdb_key", "")), scenes=q("kind='scene'"),
                   matched=q("kind='scene' AND tpdb_id IS NOT NULL"), unmatched=q("kind='scene' AND tpdb_id IS NULL"))

# ---------- smart poster crop (scenes): horizontal focus for wide covers, computed in memory, nothing stored but the number ----------
FOC = {"running": False, "kind": "focus"}
FOC_LOCK = threading.Lock()
FOC_SQL = ("SELECT id, COALESCE(NULLIF(custom_poster,''), NULLIF(poster_wide,''), poster) src FROM films WHERE kind='scene' AND COALESCE(NULLIF(custom_poster,''), NULLIF(poster_wide,''), poster) IS NOT NULL "
           "AND COALESCE(notes,'') NOT LIKE '%Review by hand%' AND COALESCE(edited_fields,'') NOT LIKE '%\"poster_focus_x\"%'")

def _poster_bytes(src):
    if src.startswith("/posters/"):
        p = os.path.join(POSTERS_DIR, os.path.basename(src))
        with open(p, "rb") as fh: return fh.read(POSTER_MAX + 1)
    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 FilmTagger", "Accept": "image/*"})
    with urllib.request.urlopen(req, timeout=20) as r: return r.read(POSTER_MAX + 1)

def focus_one(c, fid, src):
    """Compute and store the focus for one scene. Returns the method name, or None on failure."""
    data = _poster_bytes(src)
    r = focusmod.compute(data) if data and len(data) <= POSTER_MAX else None
    del data
    if r is None:
        c.execute("UPDATE films SET poster_focus_src=?, poster_focus_method='failed' WHERE id=?", (src, fid)); return None
    x = None if r["method"] == "portrait" else r["x"]
    c.execute("UPDATE films SET poster_focus_x=?, poster_focus_src=?, poster_focus_method=? WHERE id=? AND COALESCE(edited_fields,'') NOT LIKE '%\"poster_focus_x\"%'",
              (x, src, r["method"], fid))
    return r["method"]

def run_focus_job(recheck):
    time.sleep(1.5)  # let the caller commit the new poster first
    c = C(); tried = set()
    try:
        while not FOC["cancel"]:
            rows = [r for r in c.execute(FOC_SQL + ("" if recheck else " AND COALESCE(poster_focus_src,'')<>COALESCE(NULLIF(custom_poster,''), NULLIF(poster_wide,''), poster)") + " ORDER BY id").fetchall()
                    if r[0] not in tried]
            if not rows:
                if FOC.pop("again", False): time.sleep(1.5); continue
                break
            FOC["total"] = FOC["done"] + len(rows)
            for fid, src in rows:
                if FOC["cancel"]: break
                tried.add(fid)
                try:
                    m = focus_one(c, fid, src)
                    FOC[m or "failed"] = FOC.get(m or "failed", 0) + 1
                except Exception as e:
                    FOC["failed"] = FOC.get("failed", 0) + 1; FOC["error"] = f"{type(e).__name__}: {e}"
                    c.execute("UPDATE films SET poster_focus_src=?, poster_focus_method='failed' WHERE id=?", (src, fid))
                c.commit(); FOC["done"] += 1
            recheck = False
    finally:
        FOC["running"] = False; FOC["finished_at"] = time.time(); c.close()

def start_focus_job(recheck=False):
    if not focusmod.available(): return None
    with FOC_LOCK:
        if FOC.get("running"): FOC["again"] = True; return False
        FOC.clear(); FOC.update(running=True, kind="focus", done=0, total=0, failed=0, error=None, cancel=False, started_at=time.time())
        threading.Thread(target=run_focus_job, args=(bool(recheck),), daemon=True).start()
        return True

def kick_focus():
    try: start_focus_job()
    except Exception: pass

@app.post("/api/focus/start")
def focus_start():
    """Smart crop for scene posters that changed or were never checked. {recheck:true} redoes all (hand-set values kept)."""
    d = (request.json or {}) if request.is_json else {}
    r = start_focus_job(bool(d.get("recheck")))
    if r is None: return jsonify(error="Smart crop needs opencv-python-headless and numpy in the app's Python"), 400
    if r is False: return jsonify(error="Smart crop is already running"), 409
    return jsonify(FOC)

@app.post("/api/focus/cancel")
def focus_cancel(): FOC["cancel"] = True; return jsonify(FOC)

@app.get("/api/focus/status")
def focus_status():
    c = C(); q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE kind='scene' AND " + w).fetchone()[0]
    return jsonify(job=FOC, available=focusmod.available(), by_method={r[0] or "unchecked": r[1] for r in c.execute(
        "SELECT poster_focus_method, COUNT(*) FROM films WHERE kind='scene' AND COALESCE(NULLIF(custom_poster,''), NULLIF(poster_wide,''), poster) IS NOT NULL GROUP BY 1")},
        hand_set=q("COALESCE(edited_fields,'') LIKE '%\"poster_focus_x\"%'"))

@app.post("/api/films/<int:fid>/focus")
def set_focus(fid):
    """{x: 0..1} sets the crop by hand (protected); {auto: true} drops the hand value and re-detects."""
    c = C(); d = request.json or {}
    row = c.execute("SELECT edited_fields, COALESCE(NULLIF(custom_poster,''), NULLIF(poster_wide,''), poster) src FROM films WHERE id=?", (fid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    ed = set(json.loads(row["edited_fields"] or "[]"))
    if d.get("auto"):
        ed.discard("poster_focus_x"); c.execute("UPDATE films SET edited_fields=? WHERE id=?", (json.dumps(sorted(ed)), fid))
        if row["src"] and focusmod.available():
            try: focus_one(c, fid, row["src"])
            except Exception as e: c.commit(); return jsonify(dict(get_film(fid).json, focus_error=f"{type(e).__name__}: {e}"))
    else:
        try: x = min(1.0, max(0.0, float(d.get("x"))))
        except (TypeError, ValueError): return jsonify(error="x must be a number from 0 to 1"), 400
        ed.add("poster_focus_x")
        c.execute("UPDATE films SET poster_focus_x=?, poster_focus_method='manual', edited_fields=?, updated_at=? WHERE id=?", (round(x, 4), json.dumps(sorted(ed)), time.time(), fid))
    c.commit(); return get_film(fid)

# ---------- refresh ThePornDB image links for matched scenes (wide cover + fresh poster URL) ----------
IMG = {"running": False, "kind": "tpdb_images"}

def run_img_job(token, ids):
    c = C(); rl = RateLimiter(2)
    try:
        for fid in ids:
            if IMG["cancel"]: break
            try:
                r = c.execute("SELECT tpdb_id, poster, poster_wide, edited_fields FROM films WHERE id=?", (fid,)).fetchone()
                rl.wait(); s = tpdb.get_scene(token, r["tpdb_id"])
                if not s: IMG["missing"] += 1
                else:
                    ed = set(json.loads(r["edited_fields"] or "[]")); f = tpdb.scene_fields(s); upd = {}
                    if f.get("poster_wide") and f["poster_wide"] != r["poster_wide"]: upd["poster_wide"] = f["poster_wide"]
                    if f.get("poster") and f["poster"] != r["poster"] and "poster" not in ed: upd["poster"] = f["poster"]
                    if upd:
                        c.execute(f"UPDATE films SET {','.join(k + '=?' for k in upd)} WHERE id=?", list(upd.values()) + [fid]); c.commit()
                        IMG["wide"] += "poster_wide" in upd; IMG["poster"] += "poster" in upd
                    else: IMG["unchanged"] += 1
            except Exception as e:
                IMG["errors"] += 1; IMG["error"] = tpdb_err(e)
            IMG["done"] += 1
    finally:
        IMG["running"] = False; IMG["finished_at"] = time.time(); c.close(); kick_focus()

@app.post("/api/tpdb/refresh-images")
def tpdb_refresh_images():
    """Re-read image links of matched scenes from ThePornDB (never for Review-by-hand scenes; hand-set posters kept)."""
    c = C(); token = db.get_setting(c, "tpdb_key", "")
    if not token: return jsonify(error="No ThePornDB API token set in Settings"), 400
    if IMG.get("running"): return jsonify(error="Already running"), 409
    ids = [r[0] for r in c.execute("SELECT id FROM films WHERE kind='scene' AND COALESCE(tpdb_id,'')<>'' AND COALESCE(notes,'') NOT LIKE '%Review by hand%' ORDER BY id")]
    IMG.clear(); IMG.update(running=True, kind="tpdb_images", done=0, total=len(ids), wide=0, poster=0, unchanged=0, missing=0, errors=0, error=None, cancel=False)
    threading.Thread(target=run_img_job, args=(token, ids), daemon=True).start()
    return jsonify(IMG)

@app.get("/api/tpdb/refresh-images")
def tpdb_refresh_images_status(): return jsonify(IMG)

# ---------- About / Check for Updates (read-only: never downloads or runs anything) ----------
UPD = {"at": 0, "data": None}
UPD_TTL = 3600

def _ver(v):
    m = _re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
    return tuple(int(x or 0) for x in m.groups()) if m else None

def check_updates(force=False):
    if not force and UPD["data"] and time.time() - UPD["at"] < UPD_TTL: return dict(UPD["data"], cached=True)
    base = dict(current=APP_VERSION, releases_url=f"https://github.com/{REPO}/releases", checked_at=time.time())
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": f"FilmTagger/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r: rel = json.loads(r.read().decode("utf-8"))
        latest = (rel.get("tag_name") or rel.get("name") or "").strip()
        lv, cv = _ver(latest), _ver(APP_VERSION)
        out = dict(base, ok=True, latest=latest.lstrip("vV"), update_available=bool(lv and cv and lv > cv),
                   notes=(rel.get("body") or "")[:8000], release_url=rel.get("html_url") or base["releases_url"],
                   published_at=rel.get("published_at"))
    except urllib.error.HTTPError as e:
        rl = e.headers.get("X-RateLimit-Remaining") == "0" or e.code == 429
        msg = ("GitHub rate limit reached; try again later" if rl or e.code == 403 else
               "No published releases found" if e.code == 404 else f"GitHub answered HTTP {e.code}")
        out = dict(base, ok=False, error=msg, update_available=False)
    except Exception as e:
        out = dict(base, ok=False, error="Could not reach GitHub (offline?): " + type(e).__name__, update_available=False)
    UPD.update(at=time.time(), data=out)
    return dict(out, cached=False)

@app.get("/api/about")
def about():
    return jsonify(name="Film Tagger", version=APP_VERSION, author="Kyle R. Hudson",
                   description="A local catalogue and tagger for your film and scene library.",
                   repo_url=f"https://github.com/{REPO}", releases_url=f"https://github.com/{REPO}/releases",
                   credits="Film data from TMDB (this product uses the TMDB API but is not endorsed or certified by TMDB). Scene data from ThePornDB.")

@app.get("/api/updates/check")
def updates_check():
    """?force=1 bypasses the one-hour cache. Only reports; the user opens the release page themselves."""
    return jsonify(check_updates(bool(request.args.get("force"))))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Film Tagger running: open http://localhost:{port}   (database: {db.DB_PATH})")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
