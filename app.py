"""Film Tagger - local film library cataloging & tagging. Run: python app.py -> http://localhost:5050"""
import os, io, csv, json, time, urllib.request, urllib.parse, urllib.error
from flask import Flask, request, jsonify, send_from_directory, Response
import db
import resolution as resmod

app = Flask(__name__, static_folder="static")
FILM_FIELDS = ["title", "year", "mpaa", "director", "cast_", "runtime", "plot", "poster", "rating", "watched", "notes", "storage", "tmdb_id", "original_title", "user_akas",
               "writers", "producers", "cinematographer", "composer", "country", "studio", "resolution"]

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
                  "notes": "f.notes LIKE ?", "tags": TAGQ, "path": PATHQ, "country": "f.country LIKE ?", "studio": "fold_in(f.studio, ?)", "resolution": "f.resolution LIKE ?"}
        for w in text.split():
            like = f"%{w}%"
            if field in single:
                where.append(single[field]); args.append(w if field in ("title", "studio") else like)
                if field == "tags": args.append(w)
            else:
                where.append(f"({TITLEQ} OR f.director LIKE ? OR f.cast_ LIKE ? OR f.plot LIKE ? OR f.notes LIKE ? OR f.country LIKE ? OR fold_in(f.studio, ?) OR f.resolution LIKE ? OR CAST(f.year AS TEXT)=? OR {PATHQ} OR {TAGQ})")
                args += [w, like, like, like, like, like, w, like, w, like, like, w]
    for key, col, op in [("year_min", "year", ">="), ("year_max", "year", "<="), ("rating_min", "rating", ">="),
                         ("rating_max", "rating", "<="), ("runtime_min", "runtime", ">="), ("runtime_max", "runtime", "<=")]:
        v = q.get(key)
        if v not in (None, ""): where.append(f"f.{col} {op} ?"); args.append(int(v))
    w = q.get("watched")
    if w in ("1", "0", 1, 0): where.append("COALESCE(f.watched,0)=?"); args.append(int(w))
    if q.get("missing") in ("1", 1): where.append("f.missing=1")
    if q.get("pending") in ("1", 1): where.append("f.id IN (SELECT film_id FROM film_tags WHERE status='pending')")
    if q.get("queued") in ("1", 1): where.append("f.id IN (SELECT film_id FROM smart_queue)")
    mp = split_list(q.get("mpaa"))
    if mp:
        parts = ["UPPER(COALESCE(f.mpaa,'')) IN (" + ",".join("?" * len(mp)) + ")"]
        args_mp = [("" if m == "(none)" else m.upper()) for m in mp]
        where.append("(" + parts[0] + ")"); args += args_mp
    lib = (q.get("lib") or "").strip().rstrip("\\/")
    if lib:
        esc_ = lib.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("f.id IN (SELECT film_id FROM files WHERE LOWER(path) LIKE ? ESCAPE '\\' OR LOWER(path) LIKE ? ESCAPE '\\')")
        args += [esc_ + "\\\\%", esc_ + "/%"]
    if q.get("nompaa") in ("1", 1): where.append("f.tmdb_id IS NOT NULL AND COALESCE(f.mpaa,'')=''")
    if q.get("needs_tmdb") in ("1", 1): where.append("f.tmdb_status='needs_match'")
    person = (q.get("person") or "").strip()
    if person:  # exact name match against stored people lists (not a text search)
        role = q.get("prole") or ""
        cols = [db.PEOPLE_ROLES[role]] if role in db.PEOPLE_ROLES else list(db.PEOPLE_ROLES.values())
        where.append("(" + " OR ".join(f"person_in(f.{col}, ?)" for col in cols) + ")"); args += [person] * len(cols)
    decs = [int(x) for x in split_list(q.get("decade")) if x.isdigit()]
    if decs:  # OR across decades
        where.append("(" + " OR ".join("(f.year>=? AND f.year<?)" for _ in decs) + ")")
        for d in decs: args += [d, d + 10]
    for cn in split_list(q.get("country")):  # exact country name (one entry of the comma-separated list)
        where.append("person_in(f.country, ?)"); args.append(cn)
    for st in split_list(q.get("studio")):  # exact studio name (one entry of the comma-separated list)
        where.append("person_in(f.studio, ?)"); args.append(st)
    rs = split_list(q.get("res"))
    if rs:  # OR across resolutions; "(none)" = not set
        parts = ["f.resolution IN (" + ",".join("?" * len([x for x in rs if x != "(none)"])) + ")"] if [x for x in rs if x != "(none)"] else []
        if "(none)" in rs: parts.append("COALESCE(f.resolution,'')=''")
        where.append("(" + " OR ".join(parts) + ")"); args += [x for x in rs if x != "(none)"]
    if q.get("untagged") in ("1", 1): where.append("f.id NOT IN (SELECT film_id FROM film_tags WHERE status='approved')")
    return ("WHERE " + " AND ".join(where)) if where else "", args

SORTS = {"title": "f.title COLLATE FOLD", "year": "f.year", "rating": "f.rating", "runtime": "f.runtime",
         "added": "f.added_at", "director": "f.director COLLATE FOLD", "country": "f.country COLLATE FOLD", "studio": "f.studio COLLATE FOLD", "resolution": "CASE f.resolution WHEN 'SD' THEN 1 WHEN '480p' THEN 2 WHEN '576p' THEN 3 WHEN '720p' THEN 4 WHEN '1080p' THEN 5 WHEN '1440p' THEN 6 WHEN '4K' THEN 7 END", "watched": "f.watched", "id": "f.id"}

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
    return films

@app.get("/api/films")
def list_films():
    c = C(); q = request.args
    where, args = build_query(q)
    sort = SORTS.get(q.get("sort"), SORTS["title"]); order = "DESC" if q.get("order") == "desc" else "ASC"
    limit = min(int(q.get("limit", 200)), 20000); offset = int(q.get("offset", 0))
    total = c.execute(f"SELECT COUNT(*) FROM films f {where}", args).fetchone()[0]
    rows = [dict(r) for r in c.execute(f"SELECT f.* FROM films f {where} ORDER BY {sort} IS NULL, {sort} {order}, f.id LIMIT ? OFFSET ?", args + [limit, offset])]
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

@app.get("/api/studios")
def studios():
    """Distinct studio (production company) names present in the library, with film counts."""
    n = {}
    for (v,) in C().execute("SELECT studio FROM films WHERE studio IS NOT NULL AND studio<>''"):
        for x in split_list(v): n[x] = n.get(x, 0) + 1
    return jsonify([dict(name=k, n=v) for k, v in sorted(n.items(), key=lambda kv: db.fold(kv[0]))])

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
        counts[r] = c.execute(f"SELECT COUNT(*) FROM films WHERE person_in({col}, ?)", (name,)).fetchone()[0]
    where, args = build_query({"person": name, "prole": role})
    rows = c.execute(f"SELECT f.id, f.title, f.year FROM films f {where} ORDER BY f.year IS NULL, f.year, f.title COLLATE FOLD", args).fetchall()
    return jsonify(name=name, role=role or None, total=len(rows), counts=counts,
                   films=[dict(id=r["id"], title=r["title"], year=r["year"]) for r in rows])

# ---------- custom poster art (stored in films.custom_poster; TMDB code only ever writes films.poster) ----------
POSTERS_DIR = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "posters")
POSTER_MAX = 15 * 1024 * 1024
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
    if request.content_length and request.content_length > POSTER_MAX + 64 * 1024:
        return jsonify(error="Image is too large (max 15 MB)"), 413
    up = request.files.get("file")
    if up:
        if (up.mimetype or "").lower() not in POSTER_TYPES: return jsonify(error="Only JPG, PNG or WebP images are allowed"), 415
        data = up.read(POSTER_MAX + 1)
        if len(data) > POSTER_MAX: return jsonify(error="Image is too large (max 15 MB)"), 413
        ext = sniff_image(data)
        if not ext: return jsonify(error="That file is not a valid JPG, PNG or WebP image"), 415
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
    c.execute("UPDATE films SET custom_poster=?, updated_at=? WHERE id=?", (val, time.time(), fid)); c.commit()
    return jsonify(dict(get_film(fid).json, poster_note=note))

@app.delete("/api/films/<int:fid>/poster")
def remove_poster(fid):
    c = C(); _drop_local_poster(c, fid)
    c.execute("UPDATE films SET custom_poster=NULL, updated_at=? WHERE id=?", (time.time(), fid)); c.commit()
    return get_film(fid)

@app.get("/api/films/<int:fid>")
def get_film(fid):
    c = C(); r = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return jsonify(error="not found"), 404
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
    f["added_at"] = f["updated_at"] = time.time()
    cur = c.execute(f"INSERT INTO films({','.join(f)}) VALUES({','.join('?'*len(f))})", list(f.values()))
    for p in split_list(d.get("paths")): c.execute("INSERT OR IGNORE INTO files(film_id,path) VALUES(?,?)", (cur.lastrowid, p))
    c.commit()
    if split_list(d.get("paths")): start_res_job([cur.lastrowid])
    return get_film(cur.lastrowid)

@app.put("/api/films/<int:fid>")
def update_film(fid):
    c = C(); d = request.json or {}
    try: f = clean(d)
    except ValueError as e: return jsonify(error=str(e)), 400
    old = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not old: return jsonify(error="not found"), 404
    ed = set(json.loads(old["edited_fields"] or "[]"))
    ed |= {k for k, v in f.items() if k != "tmdb_id" and (v or None) != (old[k] or None)}
    f["edited_fields"] = json.dumps(sorted(ed)); f["updated_at"] = time.time()
    c.execute(f"UPDATE films SET {','.join(k+'=?' for k in f)} WHERE id=?", list(f.values()) + [fid])
    if "paths" in d:  # manual edit of the catalogue's path list (does not touch disk)
        new = split_list(d["paths"]) if not isinstance(d["paths"], str) else [x.strip() for x in d["paths"].splitlines() if x.strip()]
        before = {r[0] for r in c.execute("SELECT path FROM files WHERE film_id=?", (fid,))}
        c.execute(f"DELETE FROM files WHERE film_id=? AND path NOT IN ({','.join('?'*len(new)) or 'NULL'})", [fid] + new)
        if before != {r[0] for r in c.execute("SELECT path FROM files WHERE film_id=?", (fid,))}:
            c.execute("UPDATE films SET res_checked=NULL WHERE id=?", (fid,))  # paths changed: re-detect resolution
            c.commit(); start_res_job([fid])
        for p in new:
            c.execute("INSERT OR IGNORE INTO files(film_id,path,missing) VALUES(?,?,?)", (fid, p, 0 if os.path.exists(p) else 1))
    if d.get("replace_tags") is True and isinstance(d.get("tags"), list):  # explicit full replace of approved tags: list of {name, category}
        c.execute("DELETE FROM film_tags WHERE film_id=? AND status='approved'", (fid,))
        for t in d["tags"]:
            t = {"name": t} if isinstance(t, str) else t
            db.add_tag(c, fid, t.get("name"), t.get("category"))
    c.commit(); return get_film(fid)

@app.delete("/api/films/<int:fid>")
def delete_film(fid):  # removes catalogue entry only; never touches files
    c = C(); c.execute("DELETE FROM films WHERE id=?", (fid,)); c.commit(); return ok()

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
    for fid in (request.json or {}).get("film_ids", []): c.execute("DELETE FROM films WHERE id=?", (fid,))
    c.commit(); return ok()

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
                   ai_key_set=bool(ak), ai_key_hint=("…" + ak[-4:]) if len(ak) > 8 else ("set" if ak else ""),
                   queue=c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0],
                   pending=c.execute("SELECT COUNT(DISTINCT film_id) FROM film_tags WHERE status='pending'").fetchone()[0],
                   films=c.execute("SELECT COUNT(*) FROM films").fetchone()[0], db_path=db.DB_PATH)

@app.put("/api/settings")
def settings_put():
    c = C(); d = request.json or {}
    if "roots" in d: db.set_setting(c, "roots", [r.strip() for r in d["roots"] if r.strip()])
    if "tmdb_key" in d: db.set_setting(c, "tmdb_key", d["tmdb_key"].strip().strip('"').strip("'").strip())
    if "ai_url" in d: db.set_setting(c, "ai_url", d["ai_url"].strip())
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
        r = db.scan(c, roots, SCAN); SCAN.update(r); SCAN["cancelled"] = SCAN.get("cancel", False); return jsonify(r)
    finally:
        SCAN["running"] = False; SCAN["finished_at"] = time.time()
        try: start_res_job()  # detect resolution of newly found films in the background
        except Exception: pass

SCAN = {"running": False, "kind": "scan"}

@app.post("/api/scan/cancel")
def scan_cancel(): SCAN["cancel"] = True; return jsonify(SCAN)

@app.get("/api/libraries")
def libraries():
    """Configured roots plus top-level subfolders (derived from catalogued paths, so works offline)."""
    c = C(); roots = db.get_setting(c, "roots", []); out = []
    paths = [r[0] for r in c.execute("SELECT path FROM files")]
    for root in roots:
        rp = root.rstrip("\\/"); low = rp.lower(); subs = {}; n = 0
        for p in paths:
            pl = p.lower()
            if pl.startswith(low) and len(p) > len(rp) and p[len(rp)] in "\\/":
                n += 1; rest = p[len(rp) + 1:]; parts = _re.split(r"[\\/]", rest)
                if len(parts) > 1: subs[parts[0].lower()] = (parts[0], subs.get(parts[0].lower(), (0, 0))[1] + 1)
        sep = "\\" if "\\" in rp or _re.match(r"^[A-Za-z]:", rp) else "/"
        out.append(dict(path=rp, count=n, subfolders=sorted([dict(name=v[0], path=rp + sep + v[0], count=v[1]) for v in subs.values()], key=lambda x: x["name"].lower())))
    return jsonify(out)

@app.get("/api/jobs")
def jobs(): return jsonify(job=JOB, scan=SCAN, res=RES)

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
TMDB_NEVER = {"rating", "watched", "notes", "custom_poster", "storage", "user_akas", "resolution", "edited_fields"}

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
        if studios_of(m): upd["studio"] = studios_of(m)
        upd["studio_checked"] = 1
    ed = set(json.loads((c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or "[]"))
    upd = {k: v for k, v in upd.items() if k not in ed and k not in TMDB_NEVER and k not in set(keep)}
    upd["tmdb_id"] = m["id"]; upd["tmdb_status"] = "matched"
    c.execute(f"UPDATE films SET {','.join(k+'=?' for k in upd)} WHERE id=?", list(upd.values()) + [fid])
    if genres:
        for g in m.get("genres", []): db.add_tag_if_absent(c, fid, g.get("name"), "Genre", source="tmdb")

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
                st = studios_of(m)
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

@app.post("/api/bulk/studio")
def bulk_studio():
    """{film_ids:[..], studio:"A, B"|""} - set/clear studio on many films (marked hand-edited)."""
    d = request.json or {}
    try: ids = [int(i) for i in d.get("film_ids", [])]
    except (TypeError, ValueError): return jsonify(error="bad film_ids"), 400
    v = ", ".join(dict.fromkeys(split_list(d.get("studio") or ""))) or None
    c = C(); now = time.time(); n = 0
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
        cond = "(poster IS NULL OR poster='') AND tmdb_id IS NULL" + ("" if retry else " AND COALESCE(tmdb_status,'')<>'needs_match'")
        ids = [r[0] for r in c.execute(f"SELECT id FROM films WHERE {cond} ORDER BY id")]
        JOB.clear(); JOB.update(running=True, kind="posters", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_poster_job, args=(key, ids), daemon=True).start()
    return jsonify(JOB)

@app.get("/api/posters/status")
def posters_status(): return jsonify(JOB)

# ---------- resolution detection (local files; independent of the TMDB job so both can run at once) ----------
RES = {"running": False, "kind": "resolution"}
RES_LOCK = threading.Lock()

def run_res_job(ids):
    c = C()
    try:
        for fid in ids:
            if RES["cancel"]: break
            try:
                paths = [r[0] for r in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (fid,))]
                RES["current"] = paths[0] if paths else ""
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
            ids = [r[0] for r in c.execute("SELECT id FROM films WHERE COALESCE(edited_fields,'') NOT LIKE '%\"resolution\"%'"
                   + ("" if recheck else " AND res_checked IS NULL") + " AND id IN (SELECT film_id FROM files) ORDER BY id")]
        c.close()
        RES.clear(); RES.update(running=True, kind="resolution", done=0, total=len(ids), matched=0, skipped=0, offline=0, errors=0,
                                error=None, cancel=False, started_at=time.time(), eta_s=None, current="", method=resmod.method_name())
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

@app.post("/api/films/<int:fid>/tmdb")
def tmdb_apply(fid):
    c = C(); key = db.get_setting(c, "tmdb_key", "")
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
TABLES = ["films", "files", "categories", "tags", "film_tags", "smart_queue", "saved_searches", "settings"]

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
                cat, _, name = t.rpartition(":"); db.add_tag(c, fid, name, cat or None)
        n += 1
    c.commit(); return ok(mode="merge", rows=n)

# ---------- person pages (people.py) ----------
import people as peoplemod
peoplemod.init(app, tmdb, POSTERS_DIR)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Film Tagger running: open http://localhost:{port}   (database: {db.DB_PATH})")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
