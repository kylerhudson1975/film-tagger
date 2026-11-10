"""Film Tagger - local film library cataloging & tagging. Run: python app.py -> http://localhost:5050"""
import os, io, csv, json, time, urllib.request, urllib.parse, urllib.error
from flask import Flask, request, jsonify, send_from_directory, send_file, Response
import db
import resolution as resmod
import subtitles as submod
import tpdb
import focus as focusmod
import gallery as gallerymod
import mkvtag
import metacritic as mcmod
import imdbyears as imdbmod

APP_VERSION = "1.3.5"  # keep in sync with installer/FilmTagger.nsi APPVER
REPO = "kylerhudson1975/film-tagger"

app = Flask(__name__, static_folder="static")
FILM_FIELDS = ["title", "year", "mpaa", "director", "cast_", "runtime", "plot", "poster", "rating", "watched", "notes", "storage", "tmdb_id", "original_title", "user_akas",
               "writers", "producers", "cinematographer", "composer", "country", "studio", "resolution", "primary_genre", "aznude_status", "subtitles", "release_date", "tpdb_id", "mc_slug", "imdb_id"]

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

def _ui_build():
    import hashlib
    try:
        with open(os.path.join(app.static_folder, "index.html"), "rb") as fh: return hashlib.sha1(fh.read()).hexdigest()[:12]
    except OSError: return "dev"
UI_BUILD = _ui_build()

@app.after_request
def _build_header(resp):
    """Every response names the page build the server ships, so a tab opened before an update can ask for a reload."""
    resp.headers["X-FT-Build"] = UI_BUILD
    return resp

@app.route("/")
def index():
    resp = send_from_directory(app.static_folder, "index.html")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

# ---------- films ----------
def split_list(v):
    if not v: return []
    if isinstance(v, list): return [x.strip() for x in v if x and x.strip()]
    return [x.strip() for x in v.split(",") if x.strip()]

def split_people(v):
    """Comma-separated person names; do not break on ', Jr.' / ', Sr.' / ', II' / ', III' / ', IV'."""
    import re as _re
    if not v: return []
    if isinstance(v, list): return [" ".join(str(x).split()) for x in v if x and str(x).strip()]
    # Protect generational suffixes so a plain comma-split cannot tear "Last, Jr." apart.
    mark = "\ue000"
    s = _re.sub(r",\s*((?:Jr|Sr)\.?|II|III|IV)(?=,|$)",
                lambda m: mark + m.group(1), str(v), flags=_re.I)
    return [(" ".join(p.split())).replace(mark, ", ") for p in s.split(",") if p.strip()]

def lib_clause(c, lib, col):
    """SQL '<col> IN (films with a file under library folder lib)' + args: the one folder match used by the film list
    (?lib=) and the tag counts. All scene roots count as one 'Porn Scenes' library (db.lib_prefixes)."""
    likes, args = [], []
    for pre in db.lib_prefixes(c, lib):
        esc_ = pre.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        likes += ["LOWER(path) LIKE ? ESCAPE '\\'", "LOWER(path) LIKE ? ESCAPE '\\'"]; args += [esc_ + "\\\\%", esc_ + "/%"]
    if not likes: return "0", []
    return f"{col} IN (SELECT film_id FROM files WHERE " + " OR ".join(likes) + ")", args

def build_query(q):
    where, args = [], []
    # Sidebar tag boxes (ALL / ANY / NOT). A term that names a tag (spelling-tolerant, tag_norm) matches that tag exactly;
    # a term that names no tag is a partial, case/accent-insensitive match: 'object' finds 'Object insertion'.
    T = "SELECT ft.film_id FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved' AND "
    terms = {}
    def tag_cond(name):
        k = db.tag_norm(name)
        if k not in terms:
            c0 = C()
            terms[k] = c0.execute("SELECT 1 FROM tags WHERE tag_norm(name)=? LIMIT 1", (k,)).fetchone() is not None
            c0.close()
        if terms[k]: return "tag_norm(t.name)=?", k
        return "instr(tag_norm(t.name), ?)>0", k
    for name in split_list(q.get("all")):          # AND
        cond, a = tag_cond(name); where.append(f"f.id IN ({T}{cond})"); args.append(a)
    anyl = split_list(q.get("any"))                 # OR
    if anyl:
        conds = [tag_cond(n) for n in anyl]
        where.append(f"f.id IN ({T}(" + " OR ".join(cd for cd, _ in conds) + "))"); args += [a for _, a in conds]
    for name in split_list(q.get("none")):         # NOT
        cond, a = tag_cond(name); where.append(f"f.id NOT IN ({T}{cond})"); args.append(a)
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
        lw, la = lib_clause(C(), lib, "f.id"); where.append(lw); args += la
    if q.get("nompaa") in ("1", 1): where.append("f.tmdb_id IS NOT NULL AND COALESCE(f.mpaa,'')=''")
    if q.get("needs_tmdb") in ("1", 1): where.append("f.tmdb_status='needs_match'")
    if q.get("soft_match") in ("1", 1):  # matched scenes whose file is not renamed yet (orange flag)
        c0 = C(); ids = soft_match_ids(c0); c0.close(); where.append(f"f.id IN ({','.join(map(str, ids)) or 'NULL'})")
    person = (q.get("person") or "").strip()
    if person:  # exact name match against stored people lists (not a text search)
        role = q.get("prole") or ""
        cols = [db.PEOPLE_ROLES[role]] if role in db.PEOPLE_ROLES else list(db.PEOPLE_ROLES.values())
        names = db.ALIAS_SEP.join(db.expand_person(C(), person))  # the person under any of their aliases
        where.append("(" + " OR ".join(f"person_in_any(f.{col}, ?)" for col in cols) + ")"); args += [names] * len(cols)
    cc = [int(x) for x in str(q.get("cocredit") or "").split(",") if x.strip().isdigit()][:3]
    if len(cc) >= 2:  # "Credited with": titles every listed person is credited on (same set and age rule as /api/people/cocredit)
        import people as _pm
        ids = sorted(_pm.COCREDIT["ids"](C(), cc)) if _pm.COCREDIT.get("ids") else []
        where.append(f"f.id IN ({','.join(str(int(i)) for i in ids) or 'NULL'})")
    pin = str(q.get("pin") or "").strip()
    if pin.isdigit():  # person page insights: this person's library titles after the age rule (same set the charts count)
        import people as _pm
        ids = sorted(_pm.COCREDIT["person"](C(), int(pin))) if _pm.COCREDIT.get("person") else []
        where.append(f"f.id IN ({','.join(str(int(i)) for i in ids) or 'NULL'})")
    fids = [int(x) for x in str(q.get("ids") or "").split(",") if x.strip().isdigit()][:5000]
    if fids:  # an exact list of titles (Ask a question chart segments without a matching filter)
        where.append(f"f.id IN ({','.join(map(str, fids))})")
    mo = str(q.get("month") or "").strip()
    if mo.isdigit():  # month of release (Dashboard explore): 1-12, or 0 = unknown month (same expression as the chart)
        where.append(f"{repmod.MONTH_SQL}=?"); args.append(int(mo))
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
    if q.get("noposter") in ("1", 1): where.append("COALESCE(f.custom_poster,'')='' AND COALESCE(f.poster,'')='' AND NOT (f.kind='scene' AND COALESCE(f.poster_wide,'')<>'')")
    return ("WHERE " + " AND ".join(where)) if where else "", args

# Studio sort: within a studio, first billed performer A-Z (case/accent folded; none last), then newest release, then id.
FIRST_PERFORMER = "NULLIF(TRIM(substr(COALESCE(f.cast_,''), 1, instr(COALESCE(f.cast_,'') || ',', ',') - 1)), '')"
STUDIO_TIE = f"{FIRST_PERFORMER} IS NULL, {FIRST_PERFORMER} COLLATE FOLD, f.release_date IS NULL, f.release_date DESC, "
# Release-date sort (sort key only; stored/displayed values unchanged): year-only sorts as YYYY-07-01, YYYY-MM as YYYY-MM-15.
_RD = "COALESCE(NULLIF(f.release_date,''), CASE WHEN f.year IS NOT NULL THEN printf('%04d', f.year) END)"
RELEASE_SORT = f"CASE length({_RD}) WHEN 4 THEN {_RD} || '-07-01' WHEN 7 THEN {_RD} || '-15' ELSE {_RD} END"
SORTS = {"title": "f.title COLLATE FOLD", "year": RELEASE_SORT, "release_date": RELEASE_SORT, "rating": "f.rating", "runtime": "f.runtime",
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
    sm = soft_match_ids(c, [f["id"] for f in films if f.get("kind") == "scene"])
    for f in films: f["soft_match"] = f["id"] in sm
    for f in films:  # cast character names: JSON {actor: character} -> object for the Edit hover tip
        raw = f.get("cast_roles")
        if isinstance(raw, str) and raw.strip():
            try: f["cast_roles"] = json.loads(raw) or {}
            except Exception: f["cast_roles"] = {}
        elif not isinstance(raw, dict):
            f["cast_roles"] = {}
        slug = (f.get("mc_slug") or "").strip()
        f["mc_url"] = mcmod.page_url(slug) if slug else None
        f["mc_critic_url"] = mcmod.page_url(slug, "critic") if slug else None
        f["mc_user_url"] = mcmod.page_url(slug, "user") if slug else None
        f["mc_eligible"] = (f.get("kind") or "") != "scene"  # Classic Porn is refined in get_film (needs file paths)
    return films

def soft_match_ids(c, ids=None):
    """Scenes that are a 'soft match': flagged rename_pending, or matched (tpdb_id) with a stored file path that is not the
    standard '<root>/<Studio>/<standard name>[ (n)].ext'. Same set as the Rename waiting view, but from stored paths only (no
    filesystem access). Never Review-by-hand or [Hold rename] scenes, nor ones missing studio/date/title."""
    if ids is not None and not ids: return set()
    w = "kind='scene' AND (COALESCE(rename_pending,0)=1 OR tpdb_id IS NOT NULL)"
    if ids is not None: w += f" AND id IN ({','.join(str(int(i)) for i in ids)})"
    films = {r["id"]: r for r in c.execute(f"SELECT id, studio, release_date, cast_, title, notes FROM films WHERE {w}")
             if r["studio"] and r["release_date"] and r["title"] and "review by hand" not in (r["notes"] or "").lower()
             and HOLD_RENAME.lower() not in (r["notes"] or "").lower()}
    if not films: return set()
    out = set()
    for fid, p in c.execute(f"SELECT film_id, path FROM files WHERE film_id IN ({','.join(map(str, films))})"):
        f = films[fid]; folder, base = os.path.split(p or ""); stem = os.path.splitext(base)[0]
        ok = os.path.basename(folder) == _safe(f["studio"], 60) and _re.fullmatch(_re.escape(scene_file_name(f)) + r"( \(\d+\))?", stem)
        if not ok: out.add(fid)
    return out

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

BUILTIN_GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary", "Drama", "Family", "Fantasy",
                  "History", "Horror", "Music", "Mystery", "Romance", "Science Fiction", "TV Movie", "Thriller", "War",
                  "Western", "Hardcore", "Softcore"]

@app.get("/api/primary-genres")
def primary_genres():
    """Every genre the edit menu should offer: Genre tags, genres TMDB saved on a film, primary genres already
    set, names you typed, and the standard TMDB list."""
    c = C()
    used = [dict(name=r[0], n=r[1]) for r in c.execute("SELECT primary_genre, COUNT(*) FROM films WHERE COALESCE(primary_genre,'')<>'' GROUP BY primary_genre")]
    tags = [r["name"] for r in c.execute("SELECT name, category FROM tags") if db.fold(r["category"] or "") in ("genre", "genres")]
    from_films = []
    for (raw,) in c.execute("SELECT tmdb_genres FROM films WHERE COALESCE(tmdb_genres,'') NOT IN ('','[]')"):
        try: gl = json.loads(raw)
        except Exception: continue
        if isinstance(gl, list): from_films.extend(str(x).strip() for x in gl if str(x).strip())
    custom = db.custom_primary_genres(c)
    choices = {}
    for n in tags + from_films + custom + [u["name"] for u in used] + BUILTIN_GENRES:
        if n: choices.setdefault(db.fold(n), n)
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
    """The film's current custom poster is being replaced or removed: a local file is moved to posters/backup/ (never
    deleted) and remembered for Restore previous. Uploaded posters are never deleted by the app."""
    prev = _fp_backup_current(c, fid)
    if prev is not None: db.set_setting(c, _fp_prev_key(fid), {"value": prev, "at": time.time()})
    return prev

def _poster_name(fid, ext):
    """A new file name in posters/ that is not used yet (also not in posters/backup/): an existing file is never overwritten."""
    name = f"film-{fid}-{int(time.time())}.{ext}"  # timestamp busts the browser cache
    n = 0
    while os.path.exists(os.path.join(POSTERS_DIR, name)) or os.path.exists(os.path.join(POSTER_BACKUP_DIR, name)):
        n += 1; name = f"film-{fid}-{int(time.time() * 1000)}-{n}.{ext}"
    return name

def _save_poster(c, fid, data, ext):
    os.makedirs(POSTERS_DIR, exist_ok=True)
    _drop_local_poster(c, fid)
    name = _poster_name(fid, ext)
    with open(os.path.join(POSTERS_DIR, name), "wb") as fh: fh.write(data)
    return "/posters/" + name

def _set_custom_poster(c, fid, val):
    """Store a poster the user chose (upload, URL, gallery photo) as the film's custom poster, marked hand-edited so
    no automatic job (TMDB / ThePornDB refresh, scans, scene-cast or Upgrade file) ever replaces or removes it."""
    ed = set(json.loads((c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or "[]"))
    if val: ed.add("custom_poster")
    else: ed.discard("custom_poster")
    c.execute("UPDATE films SET custom_poster=?, edited_fields=?, updated_at=? WHERE id=?", (val or None, json.dumps(sorted(ed)), time.time(), fid))

def _art_export(c, fid):
    """Copy the film's current local custom poster into its video folder(s) (artexport.py); never fails the request."""
    try:
        import artexport
        v = (c.execute("SELECT custom_poster FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or ""
        if not v.startswith("/posters/") or v.startswith("/posters/backup/"): return [dict(status="no local poster")]
        return artexport.export_poster(c, fid, os.path.join(POSTERS_DIR, os.path.basename(v)))
    except Exception as e:
        return [dict(status=f"error: {type(e).__name__}")]

@app.post("/api/films/<int:fid>/artwork/export")
def art_export_film(fid):
    """Explicit Export: copy the current custom poster into the video folder(s) (never overwrites other files)."""
    c = C()
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    return jsonify(art_export=_art_export(c, fid))

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
    _set_custom_poster(c, fid, val); c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note=note, art_export=_art_export(c, fid)))

@app.delete("/api/films/<int:fid>/poster")
def remove_poster(fid):
    c = C(); _drop_local_poster(c, fid)  # the file goes to posters/backup/ (Restore previous), never deleted
    _set_custom_poster(c, fid, None); c.commit(); kick_focus()
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
    return jsonify(dict(get_film(fid).json, poster_note="set from TMDB (previous poster backed up)", can_restore=True, art_export=_art_export(c, fid)))

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
    _set_custom_poster(c, fid, val); c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note="previous poster restored", can_restore=False, art_export=_art_export(c, fid)))

# ---------- Poster editor: crop / rotate / flip the current poster (done on the full-size original with OpenCV) ----------
PE_Q = 95             # JPEG quality of an edited poster
PE_SMALL_W = 600      # the editor warns when the result is narrower than this

def _pe_current(c, fid):
    """The poster the film shows now: custom poster, else (scenes) the wide cover, else the TMDB/TPDB poster."""
    r = c.execute("SELECT kind, custom_poster, poster_wide, poster FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return None
    return r["custom_poster"] or (r["kind"] == "scene" and r["poster_wide"]) or r["poster"] or None

def _pe_load(v):
    """Poster value -> (original bytes, ext) or (None, error). Local /posters/ files are read; http(s) is downloaded."""
    if not v: return None, "This film has no poster to edit"
    if v.startswith("/posters/"):
        p = os.path.join(POSTER_BACKUP_DIR if v.startswith("/posters/backup/") else POSTERS_DIR, os.path.basename(v))
        if not os.path.isfile(p): return None, "The poster file is missing"
        if os.path.getsize(p) > POSTER_SRC_MAX: return None, "Image is too large (max 200 MB)"
        with open(p, "rb") as fh: data = fh.read()
    elif v.lower().startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(v, headers={"User-Agent": "Mozilla/5.0 FilmTagger", "Accept": "image/*"})
            with urllib.request.urlopen(req, timeout=30) as resp: data = resp.read(POSTER_SRC_MAX + 1)
        except Exception as e: return None, f"Could not download the poster ({type(e).__name__})"
        if len(data) > POSTER_SRC_MAX: return None, "Image is too large (max 200 MB)"
    else: return None, "Unsupported poster location"
    ext = sniff_image(data)
    if not ext: return None, "The poster is not a JPG, PNG or WebP image"
    return data, ext

def pe_decode(data):
    """PNG keeps its alpha; JPG/WebP are decoded like browsers show them (EXIF orientation applied)."""
    import cv2, numpy as np
    flag = cv2.IMREAD_UNCHANGED if sniff_image(data) == "png" else cv2.IMREAD_COLOR
    img = cv2.imdecode(np.frombuffer(data, np.uint8), flag)
    if img is None: return None
    if img.ndim == 2: img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    if img.dtype != np.uint8: img = (img / 257).astype(np.uint8) if img.dtype == np.uint16 else img.astype(np.uint8)
    return img

def pe_oriented_size(w, h, rot):
    """Size of a w x h image after rot quarter-turns."""
    return (h, w) if rot % 2 else (w, h)

def pe_clamp_crop(crop, w, h):
    """crop {x,y,w,h} in pixels of the rotated/flipped image -> integer (x, y, cw, ch) clamped inside w x h (None = whole)."""
    if not crop: return 0, 0, w, h
    x = int(round(float(crop.get("x", 0)))); y = int(round(float(crop.get("y", 0))))
    cw = int(round(float(crop.get("w", w)))); ch = int(round(float(crop.get("h", h))))
    x = min(max(0, x), w - 1); y = min(max(0, y), h - 1)
    cw = min(max(1, cw), w - x); ch = min(max(1, ch), h - y)
    return x, y, cw, ch

def pe_result_size(w, h, rot, crop):
    """Pixel size of the saved poster for an original w x h image (before any 2000px shrink, and after it)."""
    ow, oh = pe_oriented_size(w, h, rot)
    _, _, cw, ch = pe_clamp_crop(crop, ow, oh)
    s = min(1.0, POSTER_FIT_PX / max(cw, ch))
    return (cw, ch), ((max(1, round(cw * s)), max(1, round(ch * s))) if s < 1 else (cw, ch))

def pe_apply(img, rot=0, flip=False, crop=None):
    """Rotate by rot quarter-turns clockwise (negative = left), then flip horizontally, then crop. Order matches the editor."""
    import cv2
    k = rot % 4
    if k == 1: img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif k == 2: img = cv2.rotate(img, cv2.ROTATE_180)
    elif k == 3: img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if flip: img = cv2.flip(img, 1)
    h, w = img.shape[:2]
    x, y, cw, ch = pe_clamp_crop(crop, w, h)
    img = img[y:y + ch, x:x + cw]
    if max(cw, ch) > POSTER_FIT_PX:
        s = POSTER_FIT_PX / max(cw, ch)
        img = cv2.resize(img, (max(1, round(cw * s)), max(1, round(ch * s))), interpolation=cv2.INTER_AREA)
    return img

def pe_encode(img, src_ext):
    import cv2
    if src_ext == "png": ok, out = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 6]); ext = "png"
    else:
        if img.ndim == 3 and img.shape[2] == 4: img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, PE_Q]); ext = "jpg"
    return (out.tobytes(), ext) if ok else (None, None)

def _pe_film(c, fid):
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    if is_review(c, fid): return jsonify(error="This scene is flagged for review by hand; its poster cannot be edited"), 403
    return None

@app.get("/api/films/<int:fid>/posteredit")
def posteredit_info(fid):
    """Size and type of the film's current poster at full resolution (the editor computes its readout from this)."""
    c = C(); bad = _pe_film(c, fid)
    if bad: return bad
    data, ext = _pe_load(_pe_current(c, fid))
    if data is None: return jsonify(error=ext), 400
    img = pe_decode(data)
    if img is None: return jsonify(error="Could not read the poster"), 400
    h, w = img.shape[:2]
    return jsonify(width=w, height=h, ext=ext, image=f"/api/films/{fid}/posteredit/image",
                   max_px=POSTER_FIT_PX, small_w=PE_SMALL_W, can_restore=db.get_setting(c, _fp_prev_key(fid)) is not None)

@app.get("/api/films/<int:fid>/posteredit/image")
def posteredit_image(fid):
    """The current poster's original bytes, served same-origin so the editor never depends on a remote host."""
    c = C(); bad = _pe_film(c, fid)
    if bad: return bad
    data, ext = _pe_load(_pe_current(c, fid))
    if data is None: return jsonify(error=ext), 400
    return Response(data, mimetype={"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext], headers={"Cache-Control": "no-store"})

@app.post("/api/films/<int:fid>/posteredit")
def posteredit_save(fid):
    """{rot: quarter-turns clockwise, flip: bool, crop: {x,y,w,h} in pixels of the rotated/flipped original}.
    Saves the result as the film's protected custom poster; the previous one goes to posters/backup/ (Restore previous)."""
    c = C(); bad = _pe_film(c, fid)
    if bad: return bad
    d = (request.json or {}) if request.is_json else {}
    try:
        rot = int(d.get("rot") or 0); flip = bool(d.get("flip")); crop = d.get("crop")
        if crop is not None and not isinstance(crop, dict): raise ValueError
        if crop: [float(crop[k]) for k in ("x", "y", "w", "h")]
    except Exception: return jsonify(error="Bad edit parameters"), 400
    data, ext = _pe_load(_pe_current(c, fid))
    if data is None: return jsonify(error=ext), 400
    img = pe_decode(data)
    if img is None: return jsonify(error="Could not read the poster"), 400
    if img.shape[0] * img.shape[1] > POSTER_SRC_PIXELS: return jsonify(error="Image has too many pixels"), 413
    out, oext = pe_encode(pe_apply(img, rot, flip, crop), ext)
    if out is None: return jsonify(error="Could not encode the edited poster"), 500
    prev = _fp_backup_current(c, fid)
    os.makedirs(POSTERS_DIR, exist_ok=True)
    name = f"film-{fid}-{int(time.time())}.{oext}"
    if os.path.exists(os.path.join(POSTERS_DIR, name)) or os.path.exists(os.path.join(POSTER_BACKUP_DIR, name)):
        name = f"film-{fid}-{int(time.time() * 1000)}.{oext}"
    with open(os.path.join(POSTERS_DIR, name), "wb") as fh: fh.write(out)
    db.set_setting(c, _fp_prev_key(fid), {"value": prev, "at": time.time()})
    ed = set(json.loads(c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()[0] or "[]")); ed.add("custom_poster")
    c.execute("UPDATE films SET custom_poster=?, edited_fields=?, updated_at=? WHERE id=?", ("/posters/" + name, json.dumps(sorted(ed)), time.time(), fid))
    c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note="edited (previous poster backed up)", can_restore=True, art_export=_art_export(c, fid)))

@app.get("/api/films/<int:fid>")
def get_film(fid):
    c = C(); r = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return jsonify(error="not found"), 404
    # Gallery detection hits the video/gallery folders on disk. On a busy or waking drive that can stall
    # for ~20-30s and pin the browser's few connections to localhost, so every UI click feels hung.
    # Serve the cached gallery_path/count immediately; refresh one film in the background.
    if request.method == "GET" and request.endpoint == "get_film":
        kick_gallery(fid)
    pending = False
    if request.method == "GET" and request.endpoint == "get_film":
        pending = kick_metacritic(fid)
    j = attach(c, [dict(r)])[0]
    j["mc_eligible"] = db.is_metacritic_film(c, fid)
    j["mc_pending"] = bool(pending)
    return jsonify(j)

def parse_tmdb_id(v):
    """Accepts 595334, '595334', or a TMDB URL like https://www.themoviedb.org/movie/595334-some-title."""
    if v in (None, ""): return None
    s = str(v).strip()
    m = _re.search(r"themoviedb\.org/(?:[a-z]{2}(?:-[A-Z]{2})?/)?movie/(\d+)", s) or _re.fullmatch(r"\s*(\d+)\s*", s)
    if not m: raise ValueError(f"Not a TMDB id or movie URL: {s}")
    return int(m.group(1))

def norm_release_date(v):
    """'1931-02-12', '1931-02' or '1931' (exact day unknown) -> stored as typed; '' -> None. Raises ValueError otherwise."""
    v = str(v or "").strip().replace("/", "-")
    if not v: return None
    m = _re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?", v)
    if not m or not 1870 <= int(m.group(1)) <= 2100: raise ValueError(f"Release date must be YYYY-MM-DD (or YYYY-MM / YYYY if the day is unknown), not {v}")
    y, mo, dd = int(m.group(1)), m.group(2), m.group(3)
    if mo is None: return f"{y:04d}"
    if not 1 <= int(mo) <= 12: raise ValueError(f"Release date month must be 01-12, not {v}")
    if dd is None: return f"{y:04d}-{int(mo):02d}"
    import datetime as _dt
    try: return _dt.date(y, int(mo), int(dd)).isoformat()
    except ValueError: raise ValueError(f"Not a real date: {v}")

def clean(d):
    out = {}
    for k in FILM_FIELDS:
        if k in d:
            v = d[k]
            if k == "tmdb_id": out[k] = parse_tmdb_id(v); continue
            if k == "imdb_id":
                s = "" if v is None else str(v).strip()
                if not s:
                    out[k] = None
                    continue
                iid = imdbmod.parse_imdb_id(s)
                if not iid:
                    raise ValueError(f"Not an IMDb id or imdb.com/title/… link: {s}")
                out[k] = iid
                continue
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
            if k == "mc_slug": v = mcmod.parse_slug(v)
            if k == "release_date": v = norm_release_date(v)
            if k == "resolution": v = resmod.normalize(v if isinstance(v, str) else "")
            if k == "user_akas":  # one title per line (semicolons also split); titles may contain commas
                parts = v if isinstance(v, list) else (v or "").replace(";", "\n").splitlines()
                seen = []
                for t in (" ".join(x.split()) for x in parts):
                    if t and t not in seen: seen.append(t)
                v = "\n".join(seen) or None
            out[k] = v
    if out.get("release_date"): out["year"] = int(out["release_date"][:4])  # year always follows the release date
    return out

@app.post("/api/films")
def create_film():
    c = C(); d = request.json or {}; f = clean(d); f.setdefault("title", "Untitled")
    if f.get("primary_genre"):  # typed in the new-film window: remember it and protect it from auto fill
        f["primary_genre"] = db.remember_primary_genre(c, f["primary_genre"]); f["edited_fields"] = json.dumps(["primary_genre"])
    f["added_at"] = f["updated_at"] = time.time()
    cur = c.execute(f"INSERT INTO films({','.join(f)}) VALUES({','.join('?'*len(f))})", list(f.values()))
    for p in split_list(d.get("paths")): c.execute("INSERT OR IGNORE INTO files(film_id,path) VALUES(?,?)", (cur.lastrowid, p))
    if "mpaa" in f:  # Classic Porn standing rule needs file paths first
        f["mpaa"] = db.classic_porn_mpaa(c, cur.lastrowid, f.get("mpaa"))
        c.execute("UPDATE films SET mpaa=? WHERE id=?", (f["mpaa"], cur.lastrowid))
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
    if "mpaa" in f:  # Classic Porn standing rule: NC-17 -> X
        f["mpaa"] = db.classic_porn_mpaa(c, fid, f.get("mpaa"))
    if "release_date" in f and not f["release_date"] and "year" not in d: f.pop("year", None)  # clearing the date keeps the year
    changed = {k for k, v in f.items() if k != "tmdb_id" and (v or None) != (old[k] or None)}
    if changed & {"year", "release_date"}: changed |= {"year", "release_date"}  # date and year are protected together
    if "mc_slug" in changed and not f.get("mc_slug"):
        ed.discard("mc_slug"); changed.discard("mc_slug")  # clearing the slug lets auto-search run again
    if "imdb_id" in changed and not f.get("imdb_id"):
        ed.discard("imdb_id"); changed.discard("imdb_id")  # clearing the id lets backfill / TMDB fill it again
    ed |= changed
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
    slug_changed = "mc_slug" in f and (f.get("mc_slug") or None) != (old["mc_slug"] or None)
    if slug_changed:
        kick_metacritic(fid, force=True)
    elif changed & {"title", "year", "release_date"} and "mc_slug" not in set(json.loads(old["edited_fields"] or "[]")):
        # title/year changed and the slug was not set by hand: drop the auto match and look it up again
        if db.is_metacritic_film(c, fid):
            c.execute("UPDATE films SET mc_slug=NULL, mc_metascore=NULL, mc_critic_count=NULL, mc_user_score=NULL, mc_user_count=NULL, mc_fetched_at=NULL, mc_match_ver=NULL WHERE id=?", (fid,))
            c.commit(); kick_metacritic(fid, force=True)
    if "poster" in changed and old["kind"] == "scene": kick_focus()
    rename_note = mkv = None
    if old["kind"] == "scene" and not changed & set(SCENE_NAME_FIELDS): relink_standard_named(c, fid)  # clears a stuck soft match on any Save
    if old["kind"] == "scene" and changed & set(SCENE_NAME_FIELDS):  # Rename on Save (an .mkv also gets the edited title)
        try: _, rename_note, mkv = rename_scene_file(c, fid)
        except OSError as e: rename_note = f"Saved, but the file could not be renamed: {e}"
    elif old["kind"] != "scene" and changed & {"title", "year"}:
        try:
            _, rename_note, mkv = rename_movie_file(c, fid)
        except OSError as e:
            rename_note = f"Saved, but the file could not be renamed: {e}"
            mkv = movie_mkv_after(c, fid)
    if mkv and mkv["status"] != "updated" and not rename_note: rename_note = mkv["note"]
    if rename_note and not new_tid:
        j = get_film(fid).get_json(); j["rename_note"] = rename_note; return jsonify(j)
    if new_tid:  # new/changed TMDB id on Save: load its details now (hand edits + this save's changes are kept; tags only added)
        err = fetch_and_apply(c, fid, new_tid, keep=changed)
        r = get_film(fid); j = r.get_json(); j["tmdb_loaded"] = not err
        if err: j["tmdb_error"] = err
        return jsonify(j)
    return get_film(fid)

@app.delete("/api/films/<int:fid>")
def delete_film(fid):  # removes the library entry only; never touches files
    c = C()
    if request.args.get("exclude", "1") == "0": c.execute("DELETE FROM films WHERE id=?", (fid,))
    else: db.exclude_film(c, fid)  # remembered so scans never re-add it; the file itself is left alone
    c.commit(); return ok()

# ---------- Delete files & remove from library: whole film folder when safe, else own video(s)+gallery; permanent ----------
def _df_roots(c):
    rs = list(db.get_setting(c, "roots", []) or []) + list(db.get_setting(c, "scene_roots", []) or [])
    return [r for r in dict.fromkeys(rs) if r and str(r).strip()]

def _df_norm(p): return os.path.normcase(os.path.realpath(os.path.abspath(p)))

def _df_is_drive_root(p):
    """True for C:\\, D:\\, /, etc."""
    ap = os.path.abspath(p)
    return os.path.dirname(ap) == ap

def _df_is_link(p):
    try:
        if os.path.islink(p): return True
        if hasattr(os.path, "isjunction") and os.path.isjunction(p): return True
    except OSError:
        pass
    return False

def df_inside_roots(p, roots):
    """True when p lies strictly inside one of the library roots (never a root itself)."""
    np_ = _df_norm(p)
    for r in roots:
        nr = _df_norm(r)
        try:
            if np_ != nr and os.path.commonpath([np_, nr]) == nr: return True
        except ValueError: continue  # different drives
    return False

def _df_protected_dirs():
    """(path, reason) pairs that must never be wiped as a film folder."""
    home = os.path.abspath(os.path.expanduser("~"))
    out = [
        (home, "That is your user profile"),
        (os.path.join(home, "Desktop"), "That is the Desktop"),
        (db.BASE, "That is the Film Tagger install folder"),
        (db.user_data_dir(), "That is the Film Tagger Data folder"),
    ]
    la = os.environ.get("LOCALAPPDATA")
    if la:
        out.append((os.path.join(la, "Film Tagger"), "That is the Film Tagger install folder"))
        out.append((os.path.join(la, "Film Tagger Data"), "That is the Film Tagger Data folder"))
    return out

def df_folder_guard(folder, roots, others):
    """None when this folder may be wiped for Delete files; else a short reason (caller falls back to file-only)."""
    if not folder or not str(folder).strip():
        return "No folder"
    if _df_is_link(folder):
        return "That folder is a link"
    if _df_is_drive_root(folder):
        return "That is a drive root"
    if not os.path.isdir(folder):
        return "Folder not found (drive unplugged or already gone)"
    nf = _df_norm(folder)
    for r in roots:
        if nf == _df_norm(r):
            return "That is a library folder"
    if not df_inside_roots(folder, roots):
        return "Not inside a library folder"
    for prot, why in _df_protected_dirs():
        try:
            if prot and nf == _df_norm(prot):
                return why
        except OSError:
            continue
    sep = os.sep
    for o in others:
        if o == nf or o.startswith(nf + sep):
            return "Another library item has files in that folder"
    return None

def _df_dir_size(d):
    """File count + bytes under d; never follows symlinks/junctions out of the tree."""
    n = total = 0
    for base, dirs, names in os.walk(d, topdown=True, followlinks=False):
        dirs[:] = [x for x in dirs if not _df_is_link(os.path.join(base, x))]
        for x in names:
            fp = os.path.join(base, x)
            if _df_is_link(fp):
                continue
            try:
                total += os.path.getsize(fp); n += 1
            except OSError:
                pass
    return n, total

def _df_files_plan(c, fid, roots, mine, others):
    """Today's behaviour: the item's own video file(s) + gallery folders."""
    out = []
    for i, p in enumerate(mine):
        it = dict(key=f"v{i}", kind="video", path=p, size=None, count=1, ok=False, reason=None)
        if not df_inside_roots(p, roots): it["reason"] = "Not inside a library folder"
        elif not os.path.isfile(p): it["reason"] = "File not found (drive unplugged or already gone)"
        elif _df_norm(p) in others: it["reason"] = "This file also belongs to another item"
        else: it.update(ok=True, size=os.path.getsize(p))
        out.append(it)
        kind, g = gallerymod.find(p, db.VIDEO_EXT)
        if not g: continue
        gi = dict(key=f"g{i}", kind="gallery", path=g, size=None, count=0, ok=False, reason=None)
        ng = _df_norm(g)
        if not df_inside_roots(g, roots): gi["reason"] = "Not inside a library folder"
        elif any(o == ng or o.startswith(ng + os.sep) for o in others): gi["reason"] = "This folder holds another item's files"
        elif any(os.path.splitext(x)[1].lower() in db.VIDEO_EXT for _, _, ns in os.walk(g) for x in ns):
            gi["reason"] = "This folder contains video files"
        else:
            gi["count"], gi["size"] = _df_dir_size(g); gi["ok"] = True
        out.append(gi)
    return out

def df_plan(c, fid):
    """What 'Delete files' would delete: prefer the film's containing folder(s); fall back to own video(s)+gallery.
    Returns (mode, folder_reason, items). mode is 'folder' or 'files'."""
    roots = _df_roots(c)
    mine = [r["path"] for r in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY path", (fid,))]
    others = {_df_norm(r["path"]) for r in c.execute("SELECT path FROM files WHERE film_id<>? OR film_id IS NULL", (fid,))}
    # Unique containing folders (abspath, not realpath — we delete the path as filed, without following links).
    folders, seen = [], set()
    for p in mine:
        folder = os.path.abspath(os.path.dirname(p))
        key = os.path.normcase(folder)
        if key in seen: continue
        seen.add(key); folders.append(folder)
    folder_reason = None
    if not folders:
        folder_reason = "No folder"
    else:
        for folder in folders:
            folder_reason = df_folder_guard(folder, roots, others)
            if folder_reason:
                break
    if not folder_reason:
        items = []
        for i, folder in enumerate(folders):
            count, size = _df_dir_size(folder)
            items.append(dict(key=f"f{i}", kind="folder", path=folder, size=size, count=count, ok=True, reason=None))
        return "folder", None, items
    return "files", folder_reason, _df_files_plan(c, fid, roots, mine, others)

def _rmtree_nofollow(path):
    """Permanently delete a directory tree without following symlinks or junctions out of it."""
    if _df_is_link(path):
        os.unlink(path); return
    for base, dirs, names in os.walk(path, topdown=False, followlinks=False):
        for name in names:
            fp = os.path.join(base, name)
            try: os.unlink(fp)
            except OSError:
                if os.path.isdir(fp) and not _df_is_link(fp):
                    _rmtree_nofollow(fp)
                else:
                    raise
        for name in dirs:
            dp = os.path.join(base, name)
            if _df_is_link(dp):
                os.unlink(dp)
            else:
                try: os.rmdir(dp)
                except OSError:
                    if os.path.isdir(dp): _rmtree_nofollow(dp)
    if _df_is_link(path):
        os.unlink(path)
    else:
        os.rmdir(path)

def delete_paths(paths):
    """Permanently delete each path (file, or folder with its contents; never follows links). Returns {path: None (deleted) or error text}."""
    res = {}
    for p in paths:
        try:
            if _df_is_link(p):
                os.unlink(p)
            elif os.path.isdir(p):
                _rmtree_nofollow(p)
            elif os.path.lexists(p):
                os.remove(p)
            else:
                res[p] = "Not found"; continue
            res[p] = None if not os.path.lexists(p) else "Still there after deleting"
        except OSError as e:
            res[p] = f"Could not delete ({e.strerror or type(e).__name__})"
    return res

def _retire_local_poster(cp):
    """Move a film's local custom poster (Film Tagger Data/posters/) into posters/backup/ — same as Find Posters helpers."""
    if not cp or not str(cp).startswith("/posters/") or str(cp).startswith("/posters/backup/"):
        return
    src = os.path.join(POSTERS_DIR, os.path.basename(cp))
    if not os.path.isfile(src):
        return
    os.makedirs(POSTER_BACKUP_DIR, exist_ok=True)
    name = os.path.basename(cp)
    if os.path.exists(os.path.join(POSTER_BACKUP_DIR, name)):
        name = f"{int(time.time())}-{name}"
    os.replace(src, os.path.join(POSTER_BACKUP_DIR, name))

@app.get("/api/films/<int:fid>/deletefiles")
def deletefiles_list(fid):
    c = C()
    f = c.execute("SELECT id, title, kind FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return jsonify(error="not found"), 404
    mode, folder_reason, items = df_plan(c, fid)
    return jsonify(id=fid, title=f["title"], mode=mode, folder_reason=folder_reason, items=items)

@app.post("/api/films/<int:fid>/deletefiles")
def deletefiles_do(fid):
    """{keys:[...]} from the list above. Permanently deletes those (only ones the server itself lists as deletable), then
    removes the item from the library like Remove does. Prefer the whole containing folder when safe; otherwise only the
    item's own video(s)+gallery. If a deletion fails, the failures are listed and the item is kept only while one of its
    ticked video files (or, in folder mode, any of its video files) still exists."""
    c = C()
    row = c.execute("SELECT id, custom_poster FROM films WHERE id=?", (fid,)).fetchone()
    if not row: return jsonify(error="not found"), 404
    keys = ((request.json or {}).get("keys") if request.is_json else None) or []
    if not isinstance(keys, list): return jsonify(error="Bad request"), 400
    mode, folder_reason, items = df_plan(c, fid)
    plan = {it["key"]: it for it in items}
    bad = [k for k in keys if k not in plan or not plan[k]["ok"]]
    if bad: return jsonify(error="Some of those files can't be deleted: " + ", ".join(plan[k]["path"] if k in plan else str(k) for k in bad)), 400
    keys = list(dict.fromkeys(keys)); paths = [plan[k]["path"] for k in keys]
    videos = [r["path"] for r in c.execute("SELECT path FROM files WHERE film_id=?", (fid,))]
    if mode == "files":
        videos = [plan[k]["path"] for k in keys if plan[k]["kind"] == "video"]
    cp = row["custom_poster"]
    db.exclude_film(c, fid)  # remembered (path, size, hash) while the files still exist; rolled back if the item is kept
    res = delete_paths(paths) if paths else {}
    failed = [dict(path=p, error=e) for p, e in res.items() if e]; deleted = [p for p, e in res.items() if not e]
    if not failed:
        _retire_local_poster(cp)
        c.commit()
        return ok(deleted=deleted, removed=True, mode=mode, folder_reason=folder_reason)
    keep = any(os.path.exists(v) for v in videos)
    if keep:
        c.rollback()
    else:
        _retire_local_poster(cp)
        c.commit()
    return jsonify(error=("Some files could not be deleted; the video file is still there, so the item was kept in the library" if keep
                          else "Some files could not be deleted; the video file was deleted, so the item was removed from the library"),
                   failed=failed, deleted=deleted, removed=not keep, mode=mode, folder_reason=folder_reason), 500

# ---------- Upgrade file: swap one of an item's video files for a better copy (upgrade.py); the item itself is kept ----------
import upgrade as upgrademod

@app.post("/api/films/<int:fid>/upgrade-file")
def upgrade_file(fid):
    """{file_id (or old_path), path, move, dry_run}. dry_run (or check) only compares the old and new file and says what would happen.
    Otherwise: (move the new file into the old one's folder,) repoint the file record, refresh resolution / size /
    fingerprint / runtime, send the old file to the Recycle Bin. Sidecar subtitles are listed, not renamed."""
    d = (request.json or {}) if request.is_json else {}
    c = C()
    if d.get("file_id") in (None, "") and d.get("old_path"):  # the page names the file to replace by its path
        r = c.execute("SELECT id FROM files WHERE film_id=? AND path=? COLLATE NOCASE", (fid, d["old_path"])).fetchone()
        if not r: return jsonify(error="That file does not belong to this item"), 400
        d["file_id"] = r[0]
    if d.get("dry_run", True):  # nothing changes unless dry_run is false
        p, err = upgrademod.plan(c, fid, d.get("file_id"), d.get("path"))
        if err: return jsonify(error=err[0]), err[1]
        return jsonify(dict(p, dry_run=True))
    try: r, err = upgrademod.apply(c, fid, d.get("file_id"), d.get("path"), move=bool(d.get("move")))
    except OSError as e: return jsonify(error=f"Upgrade failed: {e}"), 500
    if err: return jsonify(error=err[0]), err[1]
    try: detect_subs_one(c, fid)  # hand-edited subtitles stay as they are
    except Exception: pass
    return jsonify(r)

@app.post("/api/films/<int:fid>/upgrade-file/subtitles")
def upgrade_file_subtitles(fid):
    """{file_id, old_path, paths}: rename the confirmed subtitle files named after the replaced file to match the new one."""
    d = (request.json or {}) if request.is_json else {}
    c = C()
    try: fl = int(d.get("file_id"))
    except (TypeError, ValueError): return jsonify(error="Bad request"), 400
    done, errors = upgrademod.rename_sidecars(c, fid, fl, d.get("old_path"), d.get("paths") or [])
    if done is None: return jsonify(error=errors[0]), 400
    if done:
        try: detect_subs_one(c, fid)
        except Exception: pass
    return jsonify(renamed=done, errors=errors)

@app.post("/api/films/merge")
def merge_films():
    c = C(); ids = request.json["ids"]; keep = ids[0]
    for o in ids[1:]:
        cp = c.execute("SELECT custom_poster FROM films WHERE id=?", (o,)).fetchone()
        if cp and cp[0] and not (c.execute("SELECT custom_poster FROM films WHERE id=?", (keep,)).fetchone() or [None])[0]:
            _set_custom_poster(c, keep, cp[0])  # the kept item takes over an uploaded poster instead of losing it
        c.execute("UPDATE files SET film_id=? WHERE film_id=?", (keep, o))
        c.execute("INSERT OR IGNORE INTO film_tags SELECT ?,tag_id,source,status,added_at FROM film_tags WHERE film_id=?", (keep, o))
        c.execute("DELETE FROM films WHERE id=?", (o,))
    c.commit(); return get_film(keep)

# ---------- tags ----------
@app.get("/api/tags")
def list_tags():
    c = C()
    rows = [dict(r) for r in c.execute("SELECT t.id, t.name, t.category, (SELECT COUNT(*) FROM film_tags ft WHERE ft.tag_id=t.id AND ft.status='approved') n FROM tags t ORDER BY t.category IS NULL, t.category, t.name COLLATE FOLD")]
    lib = (request.args.get("lib") or "").strip().rstrip("\\/")
    if lib:  # ?lib=: n = approved titles in that library folder, exactly what /api/films?lib=&all=<tag> returns (same folder
        # match; spelling variants that tag_norm folds together count as one tag, like the film list's tag filter); n_all = whole library
        lw, la = lib_clause(c, lib, "ft.film_id")
        cnt = dict(c.execute(f"SELECT tag_norm(t.name) k, COUNT(DISTINCT ft.film_id) FROM film_tags ft JOIN tags t ON t.id=ft.tag_id JOIN films f ON f.id=ft.film_id WHERE ft.status='approved' AND {lw} GROUP BY k", la).fetchall())
        key = dict(c.execute("SELECT id, tag_norm(name) FROM tags").fetchall())
        for r in rows: r["n_all"] = r["n"]; r["n"] = cnt.get(key.get(r["id"]), 0)
    return jsonify(rows)

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
    # Classic Porn standing rule applied per-film below (NC-17 -> X)
    c = C(); now = time.time(); n = 0
    try:
        for fid in ids:
            r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
            if not r: continue
            ed = set(json.loads(r[0] or "[]")); ed.add("mpaa")
            vv = db.classic_porn_mpaa(c, fid, v); c.execute("UPDATE films SET mpaa=?, edited_fields=?, updated_at=? WHERE id=?", (vv, json.dumps(sorted(ed)), now, fid)); n += 1
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
    kp = db.known_performers(c)
    return jsonify(items=sort_excluded([excluded_item(dict(r), sr, kp) for r in rows]))

def excluded_item(r, sr, kp=None):
    """Studio and performers: stored at removal, else from the <Studio> folder / file name (scene roots only; names known
    in the library, never the whole file name as one person)."""
    import tpdb
    in_scenes = any(db.under(r["path"], x) for x in sr)
    n = tpdb.parse_name(r["path"], kp) if in_scenes else {}
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
    c.execute("DELETE FROM excluded WHERE id=?", (xid,))
    # the same file may have been removed more than once (under other names/paths): forget those too, or the scan
    # would still skip it by size + fingerprint (or name + size) and the restore would silently add nothing
    same = 0
    if r["size"]:
        name = os.path.basename((r["path"] or "").replace("\\", "/")).lower()
        for o in c.execute("SELECT id, path, oshash FROM excluded WHERE size=?", (r["size"],)).fetchall():
            if (r["oshash"] and o["oshash"] == r["oshash"]) or os.path.basename((o["path"] or "").replace("\\", "/")).lower() == name:
                c.execute("DELETE FROM excluded WHERE id=?", (o["id"],)); same += 1
    c.commit()
    added = False
    roots = [x for x in (db.get_setting(c, "roots", []) or []) + db.scene_roots(c) if x]
    root = next((x for x in roots if db.under(r["path"], x)), None)
    if root and os.path.isdir(root):  # also when the file was moved/renamed since it was removed
        res = db.scan(c, [root]); added = res.get("added", 0) > 0
    return ok(added=added, also_forgotten=same)

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
    c = C(); fids = (request.json or {}).get("film_ids", [])
    for fid in fids:
        c.execute("INSERT OR IGNORE INTO smart_queue VALUES(?,?)", (fid, time.time()))
    c.commit()
    try: bd_n = bdmod.queue_lookup(db.connect, fids, auto=True)  # cast birth dates before tagging, ahead of the background job
    except Exception: bd_n = 0
    return ok(count=c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0], birthdate_lookups=bd_n)

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
    scenes = [str(s).rstrip("\\/") for s in (db.scene_roots(c) or [])]
    roots = [r for r in (db.get_setting(c, "roots", []) or []) if not any(os.path.normcase(str(r).rstrip("\\/")) == os.path.normcase(s) for s in scenes)]
    return jsonify(roots=roots, tmdb_key=db.get_setting(c, "tmdb_key", ""),
                   ai_url=db.get_setting(c, "ai_url", ""), ai_model=db.get_setting(c, "ai_model", ""),
                   ai_key_set=bool(ak), scene_roots=db.get_setting(c, "scene_roots", []),
                   tpdb_key_set=bool(db.get_setting(c, "tpdb_key", "")), ai_key_hint=("…" + ak[-4:]) if len(ak) > 8 else ("set" if ak else ""),
                   queue=c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0],
                   pending=c.execute("SELECT COUNT(DISTINCT film_id) FROM film_tags WHERE status='pending'").fetchone()[0],
                   films=c.execute("SELECT COUNT(*) FROM films").fetchone()[0], db_path=db.DB_PATH,
                   height_units=peoplemod.height_units(c))

@app.put("/api/settings")
def settings_put():
    c = C(); d = request.json or {}
    if "roots" in d: db.set_setting(c, "roots", [r.strip() for r in d["roots"] if r.strip()])
    if "tmdb_key" in d: db.set_setting(c, "tmdb_key", d["tmdb_key"].strip().strip('"').strip("'").strip())
    if "ai_url" in d: db.set_setting(c, "ai_url", d["ai_url"].strip())
    if "scene_roots" in d:
        scenes = [str(r).strip().rstrip("\\/") for r in d["scene_roots"] if str(r).strip()]
        db.set_setting(c, "scene_roots", scenes)
        # A scene folder is not a film library. Leaving it in the film list sends those files to TMDB.
        have = [str(r).strip().rstrip("\\/") for r in (d["roots"] if "roots" in d else (db.get_setting(c, "roots", []) or [])) if str(r).strip()]
        have = [r for r in have if not any(os.path.normcase(r) == os.path.normcase(s) for s in scenes)]
        db.set_setting(c, "roots", have)
    if d.get("tpdb_key") is not None and d.get("tpdb_key") != "":  # blank = keep existing; "-" = clear
        db.set_setting(c, "tpdb_key", "" if d["tpdb_key"].strip() == "-" else d["tpdb_key"].strip().strip('"').strip("'"))
    if "ai_model" in d: db.set_setting(c, "ai_model", d["ai_model"].strip())
    if "height_units" in d:
        if d["height_units"] not in peoplemod.HEIGHT_UNITS: return jsonify(error="height_units must be both, metric or imperial"), 400
        db.set_setting(c, "height_units", d["height_units"])
    if d.get("ai_key") is not None and d.get("ai_key") != "":  # blank = keep existing; "-" = clear
        db.set_setting(c, "ai_key", "" if d["ai_key"].strip() == "-" else d["ai_key"].strip().strip('"').strip("'"))
    c.commit(); return settings_get()

def _ps_files(root):
    """Every file under root, via PowerShell, when Python's own directory walk sees nothing."""
    if os.name != "nt": return []
    env = os.environ.copy(); env["FT_ROOT"] = root
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-ChildItem -LiteralPath $env:FT_ROOT -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }"],
            capture_output=True, timeout=600, env=env)
    except Exception:
        return []
    raw = r.stdout or b""
    if raw.startswith(b"\xff\xfe") or (len(raw) > 3 and raw[1:2] == b"\x00"):
        text = raw.decode("utf-16", "replace")
    else:
        text = raw.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]

def catalog_scene_folders(c, folders, progress):
    """Add every video under each scene folder, including every studio subfolder. Scene folders are never film libraries."""
    import re
    now = time.time(); report = []; added = found = 0
    for root in folders or []:
        root = str(root or "").strip().rstrip("\\/")
        item = {"path": root, "videos": 0, "folders": 0, "added": 0, "already": 0, "excluded": 0, "subfolders": [], "error": None, "errors": []}
        if not root or not os.path.isdir(root):
            item["error"] = "Not a folder Film Tagger can open: " + root
            report.append(item); continue
        try:
            names = os.listdir(root)
            item["subfolders"] = sorted(n for n in names if not n.startswith((".", "$")) and os.path.isdir(os.path.join(root, n)))[:20]
        except OSError as e:
            item["error"] = "Cannot read folder: " + str(e)
            report.append(item); continue
        paths = []
        for dp, dns, fns in os.walk(root):
            if progress.get("cancel"): break
            dns[:] = [d for d in dns if not d.startswith((".", "$")) and not gallerymod.is_gallery_dir(d)]
            item["folders"] += 1
            for fn in fns:
                if os.path.splitext(fn)[1].lower() in db.VIDEO_EXT and not re.search(r"\bsample\b", fn, re.I):
                    paths.append(os.path.join(dp, fn))
        if not paths and not progress.get("cancel"):
            for p in _ps_files(root):
                if os.path.splitext(p)[1].lower() in db.VIDEO_EXT and not re.search(r"\bsample\b", os.path.basename(p), re.I):
                    paths.append(p)
            if paths: item["via"] = "powershell"
        for p in paths:
            if progress.get("cancel"): break
            item["videos"] += 1; found += 1
            if found % 20 == 0: progress.update(found=found, added=added, current=os.path.dirname(p), folders=item["folders"])
            if c.execute("SELECT 1 FROM files WHERE path=?", (p,)).fetchone():
                item["already"] += 1
                fid = c.execute("SELECT film_id FROM files WHERE path=?", (p,)).fetchone()
                if fid:
                    try: db.promote_to_scene(c, fid["film_id"], p)
                    except Exception: c.execute("UPDATE films SET kind='scene' WHERE id=? AND COALESCE(kind,'')<>'scene'", (fid["film_id"],))
                continue
            try: size = os.path.getsize(p)
            except OSError: size = None
            ex = c.execute("SELECT id, kind FROM excluded WHERE path=? COLLATE NOCASE", (p,)).fetchone()
            if ex and (ex["kind"] or "") == "scene":
                item["excluded"] += 1  # removed as a scene on purpose
                continue
            if ex: c.execute("DELETE FROM excluded WHERE id=?", (ex["id"],))  # was removed when it was filed as a film
            try:
                db.new_scene(c, p, size, root, now)
                item["added"] += 1; added += 1
                if added % 25 == 0: c.commit()
            except Exception as e:
                if len(item["errors"]) < 8: item["errors"].append(os.path.basename(p) + ": " + str(e))
        c.commit(); report.append(item)
        if progress.get("cancel"): break
    try:
        log = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "last_scene_scan.txt")
        with open(log, "w", encoding="utf-8") as fh:
            for it in report:
                fh.write(f"{it['path']}\n  videos={it['videos']} folders={it['folders']} added={it['added']} already={it.get('already')} excluded={it.get('excluded')} via={it.get('via','python')}\n  subfolders: {', '.join(it['subfolders'])}\n  error: {it['error']}\n  {it['errors']}\n")
    except OSError:
        pass
    return added, report

@app.post("/api/scan")
def scan():
    c = C(); d = request.json or {}
    if isinstance(d.get("scene_roots"), list):
        scenes = [str(r).strip().rstrip("\\/") for r in d["scene_roots"] if str(r).strip()]
        db.set_setting(c, "scene_roots", scenes); c.commit()
    else:
        scenes = db.scene_roots(c)
    roots = [str(r).strip().rstrip("\\/") for r in (d.get("roots") or db.get_setting(c, "roots", []) or []) if str(r).strip()]
    roots = [r for r in roots if not any(os.path.normcase(r) == os.path.normcase(s) for s in scenes)]  # scenes are not sent to TMDB
    db.set_setting(c, "roots", roots); c.commit()
    if SCAN["running"]: return jsonify(error="A scan is already running"), 409
    SCAN.clear(); SCAN.update(running=True, kind="scan", found=0, added=0, cancel=False, current="")
    try:
        extra, report = catalog_scene_folders(c, scenes, SCAN)
        r = db.scan(c, roots, SCAN); SCAN.update(r); SCAN["cancelled"] = SCAN.get("cancel", False)
        r["added"] = (r.get("added") or 0) + extra
        r["scene_report"] = report
        threading.Thread(target=refresh_galleries, daemon=True).start()
        return jsonify(r)
    finally:
        SCAN["running"] = False; SCAN["finished_at"] = time.time()
        try: start_res_job()
        except Exception: pass
        try: start_tpdb_job()
        except Exception: pass
        try: start_subs_job(recheck=True)
        except Exception: pass
        if not SCAN.get("cancel"):
            try: start_poster_job(only_if_any=True)
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
def jobs(): return jsonify(job=JOB, scan=SCAN, res=RES, subs=SUBS, tpdb=TP, imdb=imdbmod.STATE)

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
import subprocess, sys, base64
# Vista/Win11 folder picker (IFileOpenDialog). The old FolderBrowserDialog is only a fallback.
PS_MODERN = r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class FilmTaggerFolder {
  [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")] class FileOpenDialogRCW {}
  [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IFileOpenDialog {
    [PreserveSig] int Show(IntPtr parent);
    void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
    void SetFileTypeIndex(uint iFileType);
    void GetFileTypeIndex(out uint piFileType);
    void Advise(IntPtr pfde, out uint pdwCookie);
    void Unadvise(uint dwCookie);
    void SetOptions(uint fos);
    void GetOptions(out uint pfos);
    void SetDefaultFolder(IShellItem psi);
    void SetFolder(IShellItem psi);
    void GetFolder(out IShellItem ppsi);
    void GetCurrentSelection(out IShellItem ppsi);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
    void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
    void GetResult(out IShellItem ppsi);
    void AddPlace(IShellItem psi, uint fdap);
    void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
    void Close(int hr);
    void SetClientGuid(ref Guid guid);
    void ClearClientData();
    void SetFilter(IntPtr pFilter);
    void GetResults(out IntPtr ppenum);
    void GetSelectedItems(out IntPtr ppsai);
  }
  [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IShellItem {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out IShellItem ppsi);
    void GetDisplayName(uint sigdnName, out IntPtr ppszName);
    void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
    void Compare(IShellItem psi, uint hint, out int piOrder);
  }
  public static string Pick(string title) {
    var dlg = (IFileOpenDialog)new FileOpenDialogRCW();
    dlg.SetOptions(0x20 | 0x40 | 0x800);
    dlg.SetTitle(string.IsNullOrEmpty(title) ? "Choose a folder" : title);
    if (dlg.Show(IntPtr.Zero) != 0) return "";
    IShellItem item; dlg.GetResult(out item);
    IntPtr psz; item.GetDisplayName(0x80058000, out psz);
    string path = Marshal.PtrToStringUni(psz);
    Marshal.FreeCoTaskMem(psz);
    return path ?? "";
  }
}
"@
[Console]::Out.Write([FilmTaggerFolder]::Pick($env:FT_PICK_TITLE))
"""
TK_PICK = r"""
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    sys.exit(3)
try:
    r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
    p = filedialog.askdirectory(parent=r, title='Choose a folder', mustexist=True)
    r.destroy()
except Exception:
    sys.exit(3)
sys.stdout.write(p or '')
"""
PS_PICK = ("Add-Type -AssemblyName System.Windows.Forms;"
           "$f=New-Object System.Windows.Forms.FolderBrowserDialog;$f.Description=$env:FT_PICK_TITLE;"
           "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
           "if($f.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($f.SelectedPath)}")

def _ps(cmd, title, encoded=False):
    env = os.environ.copy(); env["FT_PICK_TITLE"] = (title or "Choose a folder")[:80]
    args = ["powershell", "-NoProfile", "-STA"]
    args += ["-EncodedCommand", cmd] if encoded else ["-Command", cmd]
    return subprocess.run(args, capture_output=True, timeout=600, env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

@app.post("/api/pick-folder")
def pick_folder():
    """Opens a native folder chooser; returns {path: str|null}. Windows uses the current Explorer dialog."""
    title = ((request.json or {}).get("title") or "Choose a folder")
    if os.name == "nt":
        try:
            enc = base64.b64encode(PS_MODERN.encode("utf-16le")).decode("ascii")
            r = _ps(enc, title, encoded=True)
            if r.returncode == 0:
                path = r.stdout.decode("utf-8", "replace").strip()
                return jsonify(path=os.path.normpath(path) if path else None)
        except Exception: pass
        try:
            r = _ps(PS_PICK, title)
            path = r.stdout.decode("utf-8", "replace").strip()
            return jsonify(path=os.path.normpath(path) if path else None)
        except Exception as e:
            return jsonify(path=None, error=f"No folder picker available ({e}); type the path instead")
    try:
        r = subprocess.run([sys.executable, "-c", TK_PICK], capture_output=True, timeout=600)
        if r.returncode == 0:
            path = r.stdout.decode("utf-8", "replace").strip() or None
            return jsonify(path=os.path.normpath(path) if path else None)
    except Exception as e:
        return jsonify(path=None, error=f"No folder picker available ({e}); type the path instead")
    return jsonify(path=None, error="No folder picker available; type the path instead")

# native file picker (video files) for Upgrade file; same approach as the folder picker
PS_PICK_FILE = ("Add-Type -AssemblyName System.Windows.Forms;"
                "$f=New-Object System.Windows.Forms.OpenFileDialog;$f.Title=$env:FT_PICK_TITLE;$f.Filter=$env:FT_PICK_FILTER;"
                "if($env:FT_PICK_DIR -and (Test-Path -LiteralPath $env:FT_PICK_DIR)){$f.InitialDirectory=$env:FT_PICK_DIR};"
                "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
                "if($f.ShowDialog($o) -eq 'OK'){[Console]::Out.Write($f.FileName)}")
TK_PICK_FILE = r"""
import sys, os
try:
    import tkinter as tk
    from tkinter import filedialog
except Exception:
    sys.exit(3)
try:
    r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True)
    d = os.environ.get('FT_PICK_DIR') or None
    p = filedialog.askopenfilename(parent=r, title='Choose the new video file', initialdir=d if d and os.path.isdir(d) else None)
    r.destroy()
except Exception:
    sys.exit(3)
sys.stdout.write(p or '')
"""

@app.post("/api/pick-file")
def pick_file():
    """Opens a native file chooser for a video file; returns {path: str|null}. {title, dir} optional."""
    d = (request.json or {}) if request.is_json else {}
    title = (d.get("title") or "Choose the new video file")
    env_dir = str(d.get("dir") or "")
    exts = ";".join("*" + e for e in sorted(db.VIDEO_EXT))
    if os.name == "nt":
        try:
            env = os.environ.copy(); env.update(FT_PICK_TITLE=title[:80], FT_PICK_DIR=env_dir, FT_PICK_FILTER=f"Video files|{exts}|All files|*.*")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", PS_PICK_FILE], capture_output=True, timeout=600, env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            path = r.stdout.decode("utf-8", "replace").strip()
            return jsonify(path=os.path.normpath(path) if path else None)
        except Exception as e:
            return jsonify(path=None, error=f"No file picker available ({e}); type or paste the path instead")
    try:
        env = os.environ.copy(); env["FT_PICK_DIR"] = env_dir
        r = subprocess.run([sys.executable, "-c", TK_PICK_FILE], capture_output=True, timeout=600, env=env)
        if r.returncode == 0:
            path = r.stdout.decode("utf-8", "replace").strip() or None
            return jsonify(path=os.path.normpath(path) if path else None)
    except Exception as e:
        return jsonify(path=None, error=f"No file picker available ({e}); type or paste the path instead")
    return jsonify(path=None, error="No file picker available; type or paste the path instead")

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
POSTER_NEVER = {"custom_poster"}  # the user's own poster: only the poster endpoints (upload, URL, gallery, Find Posters,
                                  # editor, Remove custom poster, Restore previous) ever write it; no job or refresh does
TMDB_NEVER = {"kind", "tpdb_id", "tpdb_checked", "rating", "watched", "notes", "custom_poster", "storage", "user_akas", "resolution", "subtitles", "subs_manual", "subs_checked", "edited_fields",
              "aznude_status", "aznude_checked", "aznude_url", "aznude_note",
              "mc_slug", "mc_metascore", "mc_critic_count", "mc_user_score", "mc_user_count", "mc_fetched_at", "mc_match_ver"}

def tmdb_date(d):
    """TMDB date -> stored release date; TMDB's placeholder 'YYYY-01-01' (only the year known) is stored as 'YYYY'."""
    d = (d or "")[:10] or None
    return d[:4] if d and d.endswith("-01-01") else d

def people_name_key(s):
    """Normalised key for matching cast names: accents folded, punctuation dropped, Jr/Sr unified."""
    import re as _re
    s = db.fold(s or "")
    s = _re.sub(r"\bjunior\b", "jr", s)
    s = _re.sub(r"\bsenior\b", "sr", s)
    return _re.sub(r"[^a-z0-9]+", "", s)

def cast_roles_from_credits(credits, limit=None):
    """TMDB credits.cast -> {actor name: character}. Empty characters skipped.
    limit=None means the whole cast (Film Tagger may show more than the top 10)."""
    out = {}
    rows = (credits or {}).get("cast", [])
    if limit is not None: rows = rows[:limit]
    for x in rows:
        name = (x.get("name") or "").strip(); ch = (x.get("character") or "").strip()
        if name and ch and name not in out: out[name] = ch
    return out

def tmdb_ids_for_cast_names(c, names):
    """Displayed cast name -> people.tmdb_person_id when Film Tagger already linked that person (or an alias)."""
    out = {}
    for n in names or []:
        n = (n or "").strip()
        if not n: continue
        tid = None
        for alt in db.expand_person(c, n):
            row = c.execute("SELECT tmdb_person_id FROM people WHERE name_norm=? AND merged_into IS NULL",
                            (db.fold(alt),)).fetchone()
            if row and (row[0] or 0) > 0: tid = int(row[0]); break
        if tid: out[n] = tid
    return out

def cast_roles_for_names(credits, names, tmdb_ids_by_name=None):
    """Map each displayed cast name to a character from the full TMDB cast list.
    Prefer people.tmdb_person_id when known, else a normalised name match. Keys are the displayed spellings."""
    names = [n for n in (names or []) if (n or "").strip()]
    if not names: return {}
    by_id, by_key = {}, {}
    for x in (credits or {}).get("cast", []):
        ch = (x.get("character") or "").strip()
        if not ch: continue
        tid = x.get("id")
        if tid is not None:
            try: by_id[int(tid)] = ch
            except (TypeError, ValueError): pass
        nk = people_name_key(x.get("name") or "")
        if nk and nk not in by_key: by_key[nk] = ch
    tmdb_ids_by_name = tmdb_ids_by_name or {}
    out = {}
    for n in names:
        tid = tmdb_ids_by_name.get(n)
        if tid and int(tid) in by_id: out[n] = by_id[int(tid)]; continue
        ch = by_key.get(people_name_key(n))
        if ch: out[n] = ch
    return out

def remap_cast_roles(roles, names):
    """Re-key an existing {actor: character} map onto the displayed cast spellings (normalised match, add-only)."""
    roles = dict(roles or {})
    names = [n for n in (names or []) if (n or "").strip()]
    if not names: return roles
    by_key = {}
    for k, ch in roles.items():
        ch = (ch or "").strip()
        if not ch: continue
        nk = people_name_key(k)
        if nk and nk not in by_key: by_key[nk] = ch
    out = dict(roles)
    for n in names:
        if (out.get(n) or "").strip(): continue
        ch = by_key.get(people_name_key(n))
        if ch: out[n] = ch
    return out

def cast_roles_incomplete(cast_, roles):
    """True when any displayed cast name has no non-empty character under an exact or normalised key."""
    names = split_people(cast_ or "")
    if not names: return False
    by_key = {people_name_key(k): (v or "").strip() for k, v in (roles or {}).items() if (v or "").strip()}
    return any(people_name_key(n) not in by_key for n in names)

# Job titles some .nfo files put in <actor><role> for people who are also crew. A later duplicate
# <actor> entry often has the real character; never let these block a real character name.
_CREWISH_ROLES = frozenset({
    "producer", "executive producer", "co-producer", "associate producer", "line producer",
    "writer", "screenplay", "story", "director", "assistant director", "second unit director",
    "composer", "original music composer", "music", "cinematographer", "director of photography",
    "editor", "casting", "casting director", "art director", "production designer",
    "costume designer", "makeup", "sound", "stunts", "stunt coordinator", "narrator",
})

def is_crewish_role(role):
    """True when a role string looks like a crew job title rather than a character name."""
    r = " ".join((role or "").split()).casefold()
    return bool(r) and r in _CREWISH_ROLES

def parse_nfo_roles(path):
    """Kodi/Emby .nfo next to a video: <actor><name>…</name><role>…</role>. -> {name: role}. Missing/broken file -> {}.
    Duplicate <actor> rows for the same name: keep the first real character; a later character upgrades a
    crew-job label (Producer/Writer/…); two characters keep the first."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return {}
    out = {}
    for actor in root.iter("actor"):
        name = " ".join(((actor.findtext("name") or "").split()))
        role = " ".join(((actor.findtext("role") or "").split()))
        if not name or not role: continue
        if name not in out:
            out[name] = role
        elif is_crewish_role(out[name]) and not is_crewish_role(role):
            out[name] = role
    return out

def nfo_roles_for_film(c, fid):
    """First readable .nfo beside any of the film's video files (same stem, or movie.nfo / Movie.nfo in the folder)."""
    for (path,) in c.execute("SELECT path FROM files WHERE film_id=?", (fid,)):
        if not path: continue
        folder, base = os.path.split(path); stem = os.path.splitext(base)[0]
        for cand in (os.path.join(folder, stem + ".nfo"), os.path.join(folder, "movie.nfo"), os.path.join(folder, "Movie.nfo")):
            if os.path.isfile(cand):
                roles = parse_nfo_roles(cand)
                if roles: return roles
    return {}

def merge_cast_roles(existing, incoming, only_names=None, replace_crewish=False):
    """Add-only merge. never overwrites a non-empty existing value, except when replace_crewish is True and the
    existing value is a crew job label (Producer/Writer/…) while the incoming value is a real character.
    only_names (iterable of displayed cast spellings) limits keys and rewrites incoming names onto those spellings
    via people_name_key (so 'Alfred Sauchelli, Jr.' fills the cast entry 'Alfred Sauchelli, Jr.' even after a
    naive comma split once lived in the wild)."""
    out = dict(existing or {})
    prefer = None
    if only_names is not None:
        prefer = {}
        for n in only_names:
            n = (n or "").strip()
            if n: prefer[people_name_key(n)] = n
    for name, ch in (incoming or {}).items():
        name, ch = (name or "").strip(), (ch or "").strip()
        if not name or not ch: continue
        if prefer is not None:
            k = people_name_key(name)
            if k not in prefer: continue
            name = prefer[k]
        cur = (out.get(name) or "").strip()
        if not cur:
            out[name] = ch
        elif replace_crewish and is_crewish_role(cur) and not is_crewish_role(ch):
            out[name] = ch
    return out

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
    upd = dict(title=m.get("title"), year=int(m["release_date"][:4]) if m.get("release_date") else None, release_date=tmdb_date(m.get("release_date")),
               director=", ".join(x["name"] for x in crew.get("crew", []) if x.get("job") == "Director"),
               cast_=", ".join(x["name"] for x in crew.get("cast", [])[:10]), runtime=m.get("runtime"),
               plot=m.get("overview"),
               writers=jobs("Screenplay", "Writer", "Story", "Novel", "Author"),
               producers=jobs("Producer", "Executive Producer"),
               cinematographer=jobs("Director of Photography"),
               composer=jobs("Original Music Composer", "Music", "Composer"),
               poster=m.get("poster_path") and "https://image.tmdb.org/t/p/w342" + m["poster_path"],
               mpaa=db.classic_porn_mpaa(c, fid, us_cert(m)))
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
    lock = ed | set(keep)
    if lock & {"year", "release_date"}: lock |= {"year", "release_date"}  # a hand-set year or date blocks both
    if is_scene(c, fid): lock |= {"release_date"}  # scenes keep their ThePornDB/file-name date
    upd = {k: v for k, v in upd.items() if k not in lock and k not in TMDB_NEVER}
    upd["tmdb_id"] = m["id"]; upd["tmdb_status"] = "matched"
    if upd.get("title"): upd["title"] = db.strip_title_quotes(upd["title"])
    # Character names (cast hover tip). Never overwrite a hand-locked cast_roles value.
    if "cast_roles" not in lock:
        row0 = c.execute("SELECT cast_, cast_roles FROM films WHERE id=?", (fid,)).fetchone()
        try: cur_roles = json.loads((row0["cast_roles"] if row0 else None) or "{}") or {}
        except Exception: cur_roles = {}
        cast_str = upd.get("cast_") if "cast_" in upd else ((row0["cast_"] if row0 else None) or "")
        names = split_people(cast_str)
        cur_roles = remap_cast_roles(cur_roles, names)
        ids = tmdb_ids_for_cast_names(c, names)
        incoming = cast_roles_for_names(crew, names, ids)
        incoming = merge_cast_roles(incoming, cast_roles_from_credits(crew))  # keep full-cast keys too
        roles = merge_cast_roles(cur_roles, incoming, only_names=names or None, replace_crewish=True)
        if roles != cur_roles:
            upd["cast_roles"] = json.dumps(roles, ensure_ascii=False)
        upd["cast_roles_checked"] = 1
    upd["updated_at"] = time.time()
    gnames = [g.get("name") for g in m.get("genres", []) if g.get("name")]
    if "genres" in m: upd["tmdb_genres"] = json.dumps(gnames, ensure_ascii=False)
    prev = c.execute("SELECT year, title, original_title FROM films WHERE id=?", (fid,)).fetchone()
    imdbmod.enrich_tmdb_apply(upd, lock, m,
                              film_title=prev["title"] if prev else None,
                              film_original=prev["original_title"] if prev else None)
    c.execute(f"UPDATE films SET {','.join(k+'=?' for k in upd)} WHERE id=?", list(upd.values()) + [fid])
    if "year" in upd and (prev["year"] if prev else None) != upd.get("year"):
        invalidate_metacritic_for_year(c, fid)
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
                    c.commit(); after_movie_apply(c, fid)
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

def start_poster_job(retry_skipped=False, only_if_any=False):
    """Look up films with no poster and no TMDB id. No key -> None. Already running -> False.
    only_if_any skips starting when nothing needs a lookup (used after a scan)."""
    c = C(); key = db.get_setting(c, "tmdb_key", "")
    if not key: c.close(); return None
    with JOB_LOCK:
        if JOB.get("running"): c.close(); return False
        cond = "(poster IS NULL OR poster='') AND tmdb_id IS NULL AND COALESCE(kind,'')<>'scene'" + ("" if retry_skipped else " AND COALESCE(tmdb_status,'')<>'needs_match'")
        ids = [r[0] for r in c.execute(f"SELECT id FROM films WHERE {cond} ORDER BY id")]
        c.close()
        if only_if_any and not ids: return True
        JOB.clear(); JOB.update(running=True, kind="posters", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False)
        threading.Thread(target=run_poster_job, args=(key, ids), daemon=True).start()
    return True

def run_mpaa_job(key, ids):
    rl = RateLimiter(20); c = C()
    try:
        for fid, tid in ids:
            if JOB["cancel"]: break
            try:
                rl.wait(); m = tmdb(f"movie/{tid}/release_dates", key)
                cert = db.classic_porn_mpaa(c, fid, us_cert({"release_dates": m}))
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


def _imdb_id_backfill_candidates(c):
    """Non-scene films that do not yet have an IMDb id (Classic Porn included when they have a TMDB id)."""
    like = '%"imdb_id"%'
    return list(c.execute(
        "SELECT f.id, f.title, f.original_title, f.year, f.tmdb_id, f.imdb_id, f.kind, f.edited_fields, "
        "(SELECT path FROM files WHERE film_id=f.id ORDER BY id LIMIT 1) AS path "
        "FROM films f WHERE COALESCE(f.kind,'')<>'scene' AND (f.imdb_id IS NULL OR TRIM(f.imdb_id)='') "
        "AND COALESCE(f.edited_fields,'') NOT LIKE ? ORDER BY f.id",
        (like,),
    ).fetchall())


def run_imdb_id_job(key, ids, csv_path):
    """Fill missing IMDb ids from each film's stored TMDB match (external_ids / movie.imdb_id).
    Never searches IMDb by title, never overwrites a user-set id, never changes year or file names."""
    rl = RateLimiter(4)
    c = C()
    log = []
    classic_ids = {r[0] for r in c.execute("SELECT DISTINCT film_id FROM files WHERE " + db.CLASSIC_PORN_SQL)}
    try:
        for row in ids:
            if JOB["cancel"]:
                break
            film = dict(row)
            is_classic = film["id"] in classic_ids or "classic porn" in ((film.get("path") or "").lower())
            rec = {
                "film_id": film["id"], "title": film.get("title") or "", "year": film.get("year") or "",
                "library": imdbmod.library_name(film.get("kind") or "", film.get("path"), is_classic),
                "tmdb_id": film.get("tmdb_id") or "", "imdb_id": "", "action": "skip", "reason": "",
            }
            try:
                decision = imdbmod.decide_imdb_id(film, None, require_close_year=True)
                movie = None
                if decision["reason"] == "tmdb_not_fetched" and film.get("tmdb_id"):
                    for attempt in range(4):
                        try:
                            rl.wait()
                            movie = tmdb(f"movie/{int(film['tmdb_id'])}", key, include_adult="true")
                            break
                        except urllib.error.HTTPError as e:
                            if e.code == 429:
                                time.sleep(5 * (attempt + 1))
                                continue
                            if e.code == 401:
                                JOB["error"] = "TMDB rejected the API key (401)"
                                rec["reason"] = "tmdb_unauthorized"
                                log.append(rec)
                                JOB["errors"] += 1
                                JOB["done"] += 1
                                imdbmod.write_backfill_csv(csv_path, log)
                                return
                            if e.code == 404:
                                movie = None
                                decision = {"action": "skip", "imdb_id": "", "reason": "tmdb_not_found"}
                                break
                            raise
                    else:
                        decision = {"action": "skip", "imdb_id": "", "reason": "tmdb_rate_limited"}
                    if movie is not None:
                        decision = imdbmod.decide_imdb_id(film, movie, require_close_year=True)
                rec["imdb_id"] = decision.get("imdb_id") or ""
                rec["action"] = decision["action"]
                rec["reason"] = decision["reason"]
                if decision["action"] == "fill" and decision.get("imdb_id"):
                    # Re-check we still have no id and it is not user-set, then write only imdb_id
                    cur = c.execute(
                        "UPDATE films SET imdb_id=?, updated_at=? WHERE id=? AND (imdb_id IS NULL OR TRIM(imdb_id)='') "
                        "AND COALESCE(edited_fields,'') NOT LIKE ?",
                        (decision["imdb_id"], time.time(), film["id"], '%"imdb_id"%'),
                    )
                    c.commit()
                    if cur.rowcount:
                        JOB["matched"] += 1
                    else:
                        rec["action"] = "skip"
                        rec["reason"] = "already_has_imdb_id"
                        JOB["skipped"] += 1
                else:
                    JOB["skipped"] += 1
            except urllib.error.HTTPError as e:
                rec["reason"] = f"tmdb_http_{e.code}"
                JOB["errors"] += 1
                JOB["skipped"] += 1
                if e.code == 401:
                    JOB["error"] = "TMDB rejected the API key (401)"
                    log.append(rec)
                    JOB["done"] += 1
                    break
            except Exception as e:
                rec["reason"] = f"error:{type(e).__name__}"
                JOB["errors"] += 1
                JOB["skipped"] += 1
                if JOB["errors"] >= 8 and JOB["matched"] == 0:
                    JOB["error"] = f"Stopped after repeated failures: {type(e).__name__}"
                    log.append(rec)
                    JOB["done"] += 1
                    break
            log.append(rec)
            JOB["done"] += 1
            if len(log) % 25 == 0 or JOB["done"] == JOB["total"]:
                imdbmod.write_backfill_csv(csv_path, log)
                JOB["csv"] = csv_path
        imdbmod.write_backfill_csv(csv_path, log)
        JOB["csv"] = csv_path
        JOB["log_rows"] = len(log)
    finally:
        JOB["running"] = False
        JOB["finished_at"] = time.time()
        try:
            imdbmod.write_backfill_csv(csv_path, log)
            JOB["csv"] = csv_path
        except Exception:
            pass
        try:
            c.close()
        except Exception:
            pass


@app.post("/api/imdb-ids/start")
def imdb_ids_start():
    """Backfill missing IMDb ids from each film's stored TMDB match. Does not change years or file names."""
    c = C()
    key = db.get_setting(c, "tmdb_key", "")
    if not key:
        return jsonify(error="No TMDB API key set in Settings"), 400
    d = request.json or {} if request.is_json else {}
    csv_path = (d.get("path") or "").strip() or imdbmod.default_backfill_csv_path()
    with JOB_LOCK:
        if JOB["running"]:
            return jsonify(error="Another TMDB job is already running"), 409
        rows = _imdb_id_backfill_candidates(c)
        JOB.clear()
        JOB.update(running=True, kind="imdbids", done=0, total=len(rows), matched=0, skipped=0,
                   errors=0, error=None, cancel=False, started_at=time.time(), csv=csv_path)
        threading.Thread(target=run_imdb_id_job, args=(key, rows, csv_path), daemon=True).start()
    return jsonify(JOB)


@app.get("/api/imdb-ids/status")
def imdb_ids_status():
    c = C()
    q = lambda w: c.execute("SELECT COUNT(*) FROM films WHERE " + w).fetchone()[0]
    j = dict(JOB)
    return jsonify(
        job=j,
        with_imdb_id=q("imdb_id IS NOT NULL AND TRIM(imdb_id)<>''"),
        no_imdb_id=q("(imdb_id IS NULL OR TRIM(imdb_id)='') AND COALESCE(kind,'')<>'scene'"),
        no_tmdb_id=q("(imdb_id IS NULL OR TRIM(imdb_id)='') AND tmdb_id IS NULL AND COALESCE(kind,'')<>'scene'"),
        user_set=q("""COALESCE(edited_fields,'') LIKE '%"imdb_id"%'""")
    )


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

# ---------- Cast character names (Edit-window hover tip): NFO <role> + TMDB cast[].character ----------
CRJ = dict(running=False, phase=None, done=0, total=0, filled=0, nfo=0, tmdb=0, remapped=0, skipped=0, errors=0,
           cast_filled=0, cast_searched=0, unmatched=0, started=None, finished=None, error=None, cancel=False)

def run_cast_roles_job(key, wait=0, tmdb_gap=0.3):
    """Resumable backfill: films not yet checked. NFO first (free), then TMDB for those with a tmdb_id.
    Matches characters onto the displayed cast via TMDB person id (when known) or normalised name, using the
    whole TMDB cast list (not only the top 10). Never overwrites a hand-locked cast_roles value. Gentle TMDB rate (~3/s)."""
    def work():
        time.sleep(wait)
        c = C(); rl = RateLimiter(max(1, int(1.0 / max(tmdb_gap, 0.05))))
        try:
            locked = '%"cast_roles"%'
            cast_lock = '%"cast_"%'
            todo = [r[0] for r in c.execute(
                "SELECT f.id FROM films f WHERE f.cast_roles_checked IS NULL "
                "AND COALESCE(f.edited_fields,'') NOT LIKE ? "
                "AND f.id IN (SELECT film_id FROM files) ORDER BY f.id", (locked,))]
            CRJ.update(phase="cast+roles", total=len(todo), done=0, filled=0, nfo=0, tmdb=0, remapped=0, skipped=0, errors=0,
                       cast_filled=0, cast_searched=0, unmatched=0, started=time.time(), error=None, cancel=False)
            for fid in todo:
                if CRJ.get("cancel"): break
                CRJ["done"] += 1
                try:
                    row = c.execute(
                        "SELECT cast_, cast_roles, tmdb_id, tpdb_id, title, year, edited_fields, kind FROM films WHERE id=?",
                        (fid,)).fetchone()
                    if not row: CRJ["skipped"] += 1; continue
                    ed = set(json.loads(row["edited_fields"] or "[]"))
                    if "cast_roles" in ed:
                        c.execute("UPDATE films SET cast_roles_checked=1 WHERE id=?", (fid,)); c.commit(); CRJ["skipped"] += 1; continue

                    # ---- Fill empty cast_ first (hand-locked cast_ never touched) ----
                    if "cast_" not in ed and not split_people(row["cast_"] or ""):
                        filled_cast = False
                        if (row["kind"] or "") == "scene":
                            # Scenes: ThePornDB performers only (no character names there)
                            if row["tpdb_id"]:
                                try:
                                    token = db.get_setting(c, "tpdb_token", "") or db.get_setting(c, "tpdb_key", "") or ""
                                    if token:
                                        sc = tpdb.call(f"/scenes/{row['tpdb_id']}", token)
                                        data = (sc or {}).get("data") or sc or {}
                                        perfs = [p.get("name") for p in (data.get("performers") or []) if p.get("name")]
                                        if perfs:
                                            cast_s = ", ".join(perfs)
                                            c.execute("UPDATE films SET cast_=?, updated_at=? WHERE id=? AND COALESCE(edited_fields,'') NOT LIKE ?",
                                                      (cast_s, time.time(), fid, cast_lock))
                                            if c.execute("SELECT changes()").fetchone()[0]:
                                                CRJ["cast_filled"] += 1; filled_cast = True
                                                row = dict(row); row["cast_"] = cast_s
                                except Exception:
                                    CRJ["errors"] += 1
                        elif key:
                            try:
                                tid = row["tmdb_id"]; credits = None; m = None
                                if tid:
                                    rl.wait(); m = tmdb(f"movie/{tid}", key, append_to_response="credits", include_adult="true")
                                    credits = (m or {}).get("credits") or {}
                                else:
                                    # title+year search for unmatched films (same confidence as the poster job)
                                    p = {"query": row["title"] or ""}
                                    if row["year"]: p["year"] = row["year"]
                                    rl.wait(); res = tmdb("search/movie", key, include_adult="true", **p).get("results") or []
                                    if not res and row["year"]:
                                        rl.wait(); res = tmdb("search/movie", key, include_adult="true", query=row["title"] or "").get("results") or []
                                    hit = pick_confident(row["title"], row["year"], res)
                                    CRJ["cast_searched"] += 1
                                    if hit:
                                        tid = hit["id"]
                                        rl.wait(); m = tmdb(f"movie/{tid}", key, append_to_response="credits,release_dates,alternative_titles", include_adult="true")
                                        credits = (m or {}).get("credits") or {}
                                        # apply full match (respects hand edits); cast_ comes with it
                                        apply_movie(c, fid, m, respect_edits=True, genres=True)
                                        c.commit(); after_movie_apply(c, fid)
                                        CRJ["cast_filled"] += 1; filled_cast = True
                                        row = c.execute(
                                            "SELECT cast_, cast_roles, tmdb_id, tpdb_id, title, year, edited_fields, kind FROM films WHERE id=?",
                                            (fid,)).fetchone()
                                    else:
                                        CRJ["unmatched"] += 1
                                if tid and credits is not None and not filled_cast:
                                    names0 = [x.get("name") for x in (credits.get("cast") or [])[:10] if x.get("name")]
                                    if names0:
                                        cast_s = ", ".join(names0)
                                        c.execute("UPDATE films SET cast_=?, updated_at=? WHERE id=? AND COALESCE(edited_fields,'') NOT LIKE ?",
                                                  (cast_s, time.time(), fid, cast_lock))
                                        if c.execute("SELECT changes()").fetchone()[0]:
                                            CRJ["cast_filled"] += 1; filled_cast = True
                                            row = dict(row); row["cast_"] = cast_s
                                    elif not row["tmdb_id"]:
                                        pass
                            except urllib.error.HTTPError as e:
                                if e.code == 401: CRJ["error"] = "TMDB rejected the API key (401)"; break
                                CRJ["errors"] += 1
                            except Exception as e:
                                CRJ["errors"] += 1
                                if CRJ["errors"] >= 8 and CRJ["filled"] == 0 and CRJ["cast_filled"] == 0:
                                    CRJ["error"] = f"Network/TMDB error: {e} (stopped)"; break

                    try: cur0 = json.loads(row["cast_roles"] or "{}") or {}
                    except Exception: cur0 = {}
                    names = split_people(row["cast_"] or "")
                    cur = remap_cast_roles(cur0, names)
                    remapped_only = cur != cur0
                    incoming = nfo_roles_for_film(c, fid); src = "nfo" if incoming else None
                    # TMDB characters for non-scene titles with an id (many adult titles have empty characters)
                    if row["tmdb_id"] and (row["kind"] or "") != "scene" and key and names:
                        need = (cast_roles_incomplete(row["cast_"], merge_cast_roles(cur, incoming, only_names=names or None, replace_crewish=True))
                                or cast_roles_has_crewish(row["cast_"], merge_cast_roles(cur, incoming, only_names=names or None, replace_crewish=True)))
                        if need:
                            try:
                                rl.wait(); m = tmdb(f"movie/{row['tmdb_id']}", key, append_to_response="credits", include_adult="true")
                                credits = m.get("credits") or {}
                                ids = tmdb_ids_for_cast_names(c, names)
                                troles = cast_roles_for_names(credits, names, ids)
                                troles = merge_cast_roles(troles, cast_roles_from_credits(credits))
                                if troles:
                                    incoming = merge_cast_roles(incoming, troles, replace_crewish=True)
                                    src = "tmdb" if src is None else src
                            except urllib.error.HTTPError as e:
                                if e.code == 401: CRJ["error"] = "TMDB rejected the API key (401)"; break
                                CRJ["errors"] += 1
                            except Exception as e:
                                CRJ["errors"] += 1
                                if CRJ["errors"] >= 8 and CRJ["filled"] == 0:
                                    CRJ["error"] = f"Network/TMDB error: {e} (stopped)"; break
                    roles = merge_cast_roles(cur, incoming, only_names=names or None, replace_crewish=True)
                    if roles != cur0 and roles:
                        c.execute("UPDATE films SET cast_roles=?, cast_roles_checked=1, updated_at=? WHERE id=? "
                                  "AND COALESCE(edited_fields,'') NOT LIKE ?",
                                  (json.dumps(roles, ensure_ascii=False), time.time(), fid, locked))
                        if c.execute("SELECT changes()").fetchone()[0]:
                            if src == "nfo": CRJ["nfo"] += 1; CRJ["filled"] += 1
                            elif src == "tmdb": CRJ["tmdb"] += 1; CRJ["filled"] += 1
                            elif remapped_only: CRJ["remapped"] += 1
                            else: CRJ["filled"] += 1
                        else: CRJ["skipped"] += 1
                    else:
                        c.execute("UPDATE films SET cast_roles_checked=1 WHERE id=?", (fid,)); CRJ["skipped"] += 1
                    c.commit()
                except Exception:
                    CRJ["errors"] += 1; c.rollback()
            db.set_setting(c, "cast_roles_backfill_done", time.time()); c.commit()
        except Exception as ex:
            CRJ["error"] = str(ex)[:200]
        finally:
            CRJ.update(running=False, phase="done", finished=time.time()); c.close()
    CRJ.update(running=True, error=None); threading.Thread(target=work, daemon=True).start()

def cast_roles_has_crewish(cast_, roles):
    """True when any displayed cast name is stored with a crew-job label (Producer/Writer/…) as its character."""
    names = split_people(cast_ or "")
    if not names: return False
    by_key = {people_name_key(k): (v or "").strip() for k, v in (roles or {}).items() if (v or "").strip()}
    return any(is_crewish_role(by_key[people_name_key(n)]) for n in names if people_name_key(n) in by_key)

def requeue_incomplete_cast_roles(c):
    """Clear cast_roles_checked for non-hand-locked films that still need work: missing characters,
    crew-job labels that a later NFO/TMDB character should replace, or an empty cast_ (so the job can
    fill names from TMDB / ThePornDB). Scenes are included so empty scene casts can be filled from TPDB;
    scenes still rarely get character names. Returns how many rows were requeued."""
    locked = '%"cast_roles"%'
    n = 0
    for r in c.execute(
        "SELECT id, cast_, cast_roles, edited_fields, kind FROM films WHERE id IN (SELECT film_id FROM files) "
        "AND COALESCE(edited_fields,'') NOT LIKE ?", (locked,)):
        ed = set(json.loads(r["edited_fields"] or "[]"))
        if "cast_roles" in ed: continue
        try: roles = json.loads(r["cast_roles"] or "{}") or {}
        except Exception: roles = {}
        empty_cast = not split_people(r["cast_"] or "") and "cast_" not in ed
        if empty_cast or cast_roles_incomplete(r["cast_"], roles) or cast_roles_has_crewish(r["cast_"], roles):
            c.execute("UPDATE films SET cast_roles_checked=NULL WHERE id=?", (r["id"],)); n += 1
    return n

@app.get("/api/cast_roles/job")
def cast_roles_job_status():
    c = C()
    with_roles = c.execute("SELECT COUNT(*) FROM films WHERE COALESCE(cast_roles,'') NOT IN ('', '{}')").fetchone()[0]
    locked = '%"cast_roles"%'
    pending = c.execute("SELECT COUNT(*) FROM films WHERE cast_roles_checked IS NULL AND id IN (SELECT film_id FROM files) "
                        "AND COALESCE(edited_fields,'') NOT LIKE ?", (locked,)).fetchone()[0]
    return jsonify(dict(CRJ, with_roles=with_roles, pending=pending))

@app.post("/api/cast_roles/job")
def cast_roles_job_start():
    c = C(); key = db.get_setting(c, "tmdb_key", "") or ""
    body = request.get_json(silent=True) or {}
    requeued = 0
    if body.get("force"):
        requeued = requeue_incomplete_cast_roles(c)
        db.set_setting(c, "cast_roles_backfill_done", None)
    db.set_setting(c, "cast_roles_backfill_started", time.time()); c.commit()
    if not CRJ.get("running"): run_cast_roles_job(key)
    return jsonify(dict(CRJ, requeued=requeued))

if "pytest" not in __import__("sys").modules:
    try:
        _c = C()
        if db.get_setting(_c, "cast_roles_backfill_started", None) and not db.get_setting(_c, "cast_roles_backfill_done", None):
            run_cast_roles_job(db.get_setting(_c, "tmdb_key", "") or "", wait=60)
        _c.close()
    except Exception:
        pass

@app.post("/api/posters/start")
def posters_start():
    retry = bool((request.json or {}).get("retry_skipped", False)) if request.is_json else False
    r = start_poster_job(retry)
    if r is None: return jsonify(error="No TMDB API key set in Settings"), 400
    if r is False: return jsonify(error="Another TMDB job is already running"), 409
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

def ai_chat(url, key, model, prompt, timeout=180, temperature=0.3):
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature}
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
    c.commit(); after_movie_apply(c, fid); return None

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
    c.commit(); after_movie_apply(c, fid); return get_film(fid)

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
import birthdates as bdmod
bdmod.init(app, db.connect, tmdb)
import findbirth as fbmod
fbmod.init(app, db.connect, tmdb)  # Find birth date: candidates to review (nothing saved)  # Wikidata / Wikipedia birth dates (background job + person page lookups)
import studios as studiomod
studiomod.init(app, tmdb, build_query)  # studio pages, Studios list, TMDB company / ThePornDB site backfill (background)

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

def apply_scene(c, fid, s, keep=(), override=False):
    """Apply a ThePornDB scene to a film; tags are only added. Background matching (override=False): hand-edited fields
    (edited_fields) and `keep` are never replaced. Explicit Load (override=True): hand-edited fields ARE replaced except
    `keep` (changed in this edit session) and a hand-edited poster; replaced fields are then unlocked."""
    f = tpdb.scene_fields(s)
    locked = set(json.loads((c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or "[]"))
    ed = (set(keep) | ({"poster", "poster_wide"} if "poster" in locked or "poster_wide" in locked else set())) if override else locked | set(keep)
    if ed & {"year", "release_date"}: ed |= {"year", "release_date"}  # date and year are protected together
    upd = {k: v for k, v in f.items() if k != "tags" and k not in POSTER_NEVER and v not in (None, "") and k not in ed}
    if is_review(c, fid): upd.pop("poster", None); upd.pop("poster_wide", None)  # possibly-underage review flag: never fetch images
    if upd.get("studio"): upd["studio"] = db.canon_studio(upd["studio"], c)  # 'angels.love' -> 'Angels Love'
    if upd.get("studio") and keep_parent_studio(c, fid, s, upd["studio"]): upd.pop("studio")  # never swap Kyle's studio for a sub-label
    if override:  # unlock what Load just replaced (and the id it loaded); same-session edits and a hand-edited poster stay locked
        unlock = (set(upd) | {"tpdb_id"}) - set(keep)
        if "year" in unlock or "release_date" in unlock: unlock |= {"year", "release_date"}
        upd["edited_fields"] = json.dumps(sorted(locked - unlock))
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
    apply_scene(c, fid, s, keep=[k for k in d.get("keep", []) if k in FILM_FIELDS], override=bool(sid))  # Load of a given id replaces locked fields; blank id (filename lookup) keeps them
    c.execute("UPDATE films SET rename_pending=0 WHERE id=?", (fid,)); c.commit()  # Loaded: no longer 'rename waiting'
    mkv = None
    if d.get("rename", True):
        try: mkv = rename_scene_file(c, fid)[2]
        except OSError as e: return jsonify(error=f"Matched, but the file could not be renamed: {e}"), 500
    if not mkv: return get_film(fid)
    j = get_film(fid).get_json(); j["mkv_status"] = mkv["status"]; j["mkv_note"] = mkv["note"]; return jsonify(j)

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

def relink_standard_named(c, fid):
    """A scene file renamed/replaced outside the app (e.g. re-encoded, so the scan can't match it by size+fingerprint)
    that already sits at the scene's standard name: point its missing files row there. Only when the stored path is gone,
    exactly one unclaimed standard-named video exists (any video extension) and the film has one missing row for it.
    Returns the number of rows relinked."""
    f = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not f or not all((f[k] or "").strip() for k in ("studio", "release_date", "title")): return 0
    rows = [x for x in c.execute("SELECT id, path FROM files WHERE film_id=? ORDER BY id", (fid,)).fetchall()
            if x["path"] and not os.path.exists(x["path"])]
    if len(rows) != 1: return 0
    old = rows[0]["path"]; root = next((r for r in db.scene_roots(c) if db.under(old, r)), None)
    if not root: return 0
    base = os.path.join(root, _safe(f["studio"], 60), scene_file_name(f))
    cands = [base + e for e in sorted(db.VIDEO_EXT) if os.path.isfile(base + e)]
    cands = [p for p in cands if not c.execute("SELECT 1 FROM files WHERE path=? AND id<>?", (p, rows[0]["id"])).fetchone()]
    if len(cands) != 1: return 0
    new = cands[0]
    try: size = os.path.getsize(new)
    except OSError: return 0
    c.execute("UPDATE files SET path=?, size=?, oshash=NULL, missing=0 WHERE id=?", (new, size, rows[0]["id"]))
    c.execute("UPDATE OR IGNORE sub_probe SET path=? WHERE path=?", (new, old))
    c.execute("UPDATE films SET missing=0 WHERE id=?", (fid,)); c.commit()
    try:
        with open(os.path.join(root, "_filmtagger_relinks.csv"), "a", newline="", encoding="utf-8-sig") as fh:
            csv.writer(fh).writerow([time.strftime("%Y-%m-%d %H:%M:%S"), fid, old, new])
    except OSError: pass
    return 1

def plan_scene_rename(c, fid):
    """Planned moves for one scene: ([dict(file_id, old, new, root, sidecars=[(a, b)])], note or None).
    Target: <scenes root>/<Studio>/<Studio> - YYYY-MM-DD - <Performers> - <Title>.<ext>; never overwrites (' (2)')."""
    relink_standard_named(c, fid)  # file already at its standard name but stored under an old path (stuck soft match)
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
        c.execute("UPDATE OR IGNORE sub_probe SET path=? WHERE path=?", (new, old))
        mkvtag.ensure_table(c); c.execute("UPDATE mkv_meta SET path=? WHERE file_id=? AND path=?", (new, m["file_id"], old))  # same file, new name
        c.commit(); done += 1
        with open(os.path.join(m["root"], "_filmtagger_renames.csv"), "a", newline="", encoding="utf-8-sig") as fh: csv.writer(fh).writerows(pairs)
        dd = os.path.dirname(old)
        try:
            if (not os.listdir(dd) and os.path.normcase(dd) != os.path.normcase(m["root"])
                    and os.path.basename(dd).lower() not in KEEP_DIRS): os.rmdir(dd)
        except OSError: pass
    return done

def rename_scene_file(c, fid):
    """Move a scene's file(s) (and same-name sidecars) to the standard name. Returns (renamed count, note, mkv result).
    Once the file has its standard name (hard match), .mkv files also get their container title/tags (mkv_after_rename)."""
    moves, note = plan_scene_rename(c, fid)
    n = do_scene_moves(c, moves) if moves else 0
    mkv = None
    if not note and n == len(moves):
        c.execute("UPDATE films SET rename_pending=0 WHERE id=?", (fid,)); c.commit()  # now standard
        mkv = mkv_after_rename(c, fid)
    return n, note, mkv

def movie_std_name(title, year):
    """Standard movie folder/file stem: 'Title (Year)'. Year is left out when missing."""
    t = _safe(title, 120)
    if not t: return None
    try:
        if year not in (None, ""): return f"{t} ({int(year)})"
    except (TypeError, ValueError):
        pass
    return t

_CD_SUF = _re.compile(r"(?i)(?:[\s._-])((?:CD|DISC|DISK|PART)[\s._-]*[A-Z0-9]+)$")
_WALK_UP = {"VIDEO_TS", "BDMV", "STREAM", "SUBS", "EXTRAS", "FEATURETTES", "AUDIO_TS"}

def _movie_root(c, path):
    roots = [r for r in (db.get_setting(c, "roots", []) or []) if r and str(r).strip()]
    hits = [r for r in roots if db.under(path, r)]
    return max(hits, key=lambda r: len(os.path.normcase(r.rstrip("\\/")))) if hits else None

def _film_folder(path, root):
    """Directory immediately under the library root that contains this video, or None if the file is at the root."""
    d = os.path.dirname(os.path.abspath(path))
    root = os.path.abspath(root.rstrip("\\/"))
    while os.path.basename(d).upper() in _WALK_UP and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    if os.path.normcase(d) == os.path.normcase(root):
        return None
    # first component under root
    cur, parent = d, os.path.dirname(d)
    while parent != cur and os.path.normcase(parent) != os.path.normcase(root) and db.under(parent, root):
        cur, parent = parent, os.path.dirname(parent)
    if os.path.normcase(parent) == os.path.normcase(root):
        return cur
    return d if db.under(d, root) else None

def _under_or_same(path, folder):
    if not path or not folder: return False
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return a == b or db.under(path, folder)

def _other_films_in_dir(c, folder, fid):
    if not folder: return False
    for r in c.execute("SELECT film_id, path FROM files WHERE film_id!=? AND COALESCE(missing,0)=0", (fid,)):
        if r["path"] and _under_or_same(r["path"], folder):
            return True
    return False

def _map_under(path, old_folder, new_folder):
    if not path or not old_folder: return path
    oldn = os.path.normcase(os.path.abspath(old_folder).rstrip("\\/"))
    p = os.path.abspath(path)
    pn = os.path.normcase(p)
    if pn == oldn: return new_folder
    sep = pn[len(oldn):len(oldn)+1] if len(pn) > len(oldn) else ""
    if pn.startswith(oldn) and sep in ("\\", "/", ""):
        return os.path.join(new_folder, p[len(os.path.abspath(old_folder).rstrip("\\/")):].lstrip("\\/"))
    return path

def _stem_sidecars(folder, stem, video_path, new_dir, new_stem):
    """Same-folder files whose name starts with 'stem.' (e.g. Title (2011).en.srt, .forced.srt, .nfo)."""
    import subtitles as _submod
    out = []
    try: names = os.listdir(folder)
    except OSError: return out
    vnorm = os.path.normcase(os.path.abspath(video_path))
    prefix = stem.lower() + "."
    for y in names:
        p = os.path.join(folder, y)
        if os.path.normcase(os.path.abspath(p)) == vnorm: continue
        if os.path.isdir(p):
            # '<stem> - GALLERY' (or a suffix after the stem) moves with the video
            if y.lower().startswith(stem.lower() + " - gallery") or y.lower() == stem.lower() + " - gallery":
                out.append((p, os.path.join(new_dir, new_stem + y[len(stem):]), "gallery"))
            continue
        if not y.lower().startswith(prefix): continue
        dest = os.path.join(new_dir, new_stem + y[len(stem):])
        ext = os.path.splitext(y)[1].lower()
        kind = "subtitle" if ext in getattr(_submod, "SUB_EXT", {}) else "sidecar"
        out.append((p, dest, kind))
    return out

def plan_movie_rename(c, fid, title=None, year=None):
    """Planned folder/file/sidecar moves for one movie to the standard 'Title (Year)' name.
    title/year override the stored values (used to preview an IMDb year before writing it).
    Returns (moves, error). moves is a list of dict(kind, old, new, file_id?, root). error is a reason to skip."""
    f = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return [], "not found"
    if (f["kind"] or "") == "scene": return [], "Scenes use the scene rename path"
    title = f["title"] if title is None else title
    year = f["year"] if year is None else year
    std = movie_std_name(title, year)
    if not std: return [], "Not renamed: missing title"
    files = list(c.execute("SELECT id, path FROM files WHERE film_id=? ORDER BY id", (fid,)))
    if not files: return [], None
    # Exclusive film folder (all this film's videos in one folder under a root, no other film)
    folders, roots_for = [], {}
    missing = []
    for x in files:
        old = x["path"]
        if not old or not os.path.exists(old):
            missing.append(old or "(no path)")
            continue
        root = _movie_root(c, old)
        if not root: continue
        folders.append((_film_folder(old, root), root, x))
        roots_for[id(x)] = root
    if missing and not folders:
        return [], "File missing or not reachable"
    if not folders:
        return [], "Not under a movie library folder"
    unique_folders = []
    for fol, root, x in folders:
        key = os.path.normcase(os.path.abspath(fol)) if fol else ("__root__", os.path.normcase(os.path.abspath(root)))
        if not any(k[0] == key for k in unique_folders):
            unique_folders.append((key, fol, root))
    exclusive = (len(unique_folders) == 1 and unique_folders[0][1] is not None
                 and not _other_films_in_dir(c, unique_folders[0][1], fid))
    dest_folder_by_root = {}
    folder_move = None
    if exclusive:
        fol, root = unique_folders[0][1], unique_folders[0][2]
        dest = os.path.join(root, std)
        dest_folder_by_root[os.path.normcase(os.path.abspath(root))] = dest
        if os.path.normcase(os.path.abspath(fol)) != os.path.normcase(os.path.abspath(dest)):
            if os.path.exists(dest) and os.path.normcase(os.path.abspath(dest)) != os.path.normcase(os.path.abspath(fol)):
                return [], f"Name collision: folder already exists: {dest}"
            if not db.same_drive(fol, dest):
                return [], "Not renamed: new name would be on another drive"
            folder_move = (fol, dest, root)
    out, taken = [], set()
    if folder_move:
        out.append(dict(kind="folder", old=folder_move[0], new=folder_move[1], file_id=None, root=folder_move[2]))
        taken.add(os.path.normcase(os.path.abspath(folder_move[1])))

    for x in files:
        old = x["path"]
        if not old or not os.path.exists(old): continue
        root = _movie_root(c, old)
        if not root: continue
        fol = _film_folder(old, root)
        if exclusive:
            dest_folder = dest_folder_by_root[os.path.normcase(os.path.abspath(root))]
        elif fol:
            dest_folder = fol  # shared folder: rename files in place
        else:
            dest_folder = os.path.join(root, std)  # file at library root: put it in Title (Year)\
        if not db.same_drive(old, os.path.join(dest_folder, "x")):
            return [], "Not renamed: new name would be on another drive"
        ext = os.path.splitext(old)[1]
        old_stem = os.path.splitext(os.path.basename(old))[0]
        stem = movie_std_name(title, year)
        mcd = _CD_SUF.search(old_stem.strip())
        file_stem = f"{stem} {mcd.group(1).replace('_', ' ').replace('.', ' ').strip()}" if mcd else stem
        base = os.path.join(dest_folder, file_stem)
        new = base + ext
        n = 2
        # After a folder move, the current file will already sit at dest_folder/old_basename
        after_folder = _map_under(old, folder_move[0], folder_move[1]) if folder_move else old
        while ((os.path.exists(new) and os.path.normcase(os.path.abspath(new)) != os.path.normcase(os.path.abspath(after_folder))
                and os.path.normcase(os.path.abspath(new)) != os.path.normcase(os.path.abspath(old)))
               or os.path.normcase(os.path.abspath(new)) in taken):
            # If the dest already exists and is not this file, use (2) only for a file in the same film folder
            # that is not an exclusive-folder name collision (those already returned above).
            new = f"{base} ({n}){ext}"; n += 1
        taken.add(os.path.normcase(os.path.abspath(new)))
        side = _stem_sidecars(os.path.dirname(old), old_stem, old, dest_folder, os.path.splitext(os.path.basename(new))[0])
        # drop sidecars that would collide
        side_ok = []
        for a, b, kind in side:
            b_after = b
            if os.path.exists(b) and os.path.normcase(os.path.abspath(b)) != os.path.normcase(os.path.abspath(a)) and not folder_move:
                # after folder move, a maps into dest; collision if something else is already at b
                return [], f"Name collision: {os.path.basename(b)} already exists"
            if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b_after)):
                continue
            if not db.same_drive(a, b):
                return [], "Not renamed: a sidecar would move to another drive"
            side_ok.append((a, b, kind))
        if (os.path.normcase(os.path.abspath(new)) == os.path.normcase(os.path.abspath(old))
                and not folder_move and not side_ok):
            continue
        if os.path.normcase(os.path.abspath(new)) != os.path.normcase(os.path.abspath(old)) or folder_move:
            if os.path.normcase(os.path.abspath(after_folder)) != os.path.normcase(os.path.abspath(new)):
                out.append(dict(kind="file", old=old, new=new, file_id=x["id"], root=root))
        for a, b, kind in side_ok:
            out.append(dict(kind=kind, old=a, new=b, file_id=x["id"], root=root))
    return out, None

def do_movie_moves(c, moves):
    """Execute planned movie folder/file/sidecar moves. Same logging as scene Rename on Save
    (_filmtagger_renames.csv in the library root). Returns (done file-renames, error or None).
    On failure after a folder rename, attempts to rename the folder back so year and names stay in sync."""
    import csv
    if not moves: return 0, None
    folder_mv = next((m for m in moves if m["kind"] == "folder"), None)
    done_pairs = []
    files_done = 0
    try:
        if folder_mv:
            old_f, new_f = folder_mv["old"], folder_mv["new"]
            if os.path.exists(new_f) and os.path.normcase(os.path.abspath(new_f)) != os.path.normcase(os.path.abspath(old_f)):
                return 0, f"Name collision: folder already exists: {new_f}"
            if os.path.normcase(os.path.abspath(old_f)) != os.path.normcase(os.path.abspath(new_f)):
                os.rename(old_f, new_f)
                done_pairs.append((old_f, new_f))
        def physical(old):
            return _map_under(old, folder_mv["old"], folder_mv["new"]) if folder_mv else old
        for m in moves:
            if m["kind"] == "folder": continue
            src, dst = physical(m["old"]), m["new"]
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
                continue
            if not os.path.exists(src):
                raise OSError(f"Source disappeared: {m['old']}")
            if os.path.exists(dst) and os.path.normcase(os.path.abspath(dst)) != os.path.normcase(os.path.abspath(src)):
                raise OSError(f"Name collision: {dst}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.isdir(src):
                import shutil; shutil.move(src, dst)
            else:
                os.rename(src, dst)
            done_pairs.append((m["old"], dst))
            if m["kind"] == "file" and m.get("file_id"):
                c.execute("UPDATE files SET path=?, missing=0 WHERE id=?", (dst, m["file_id"]))
                c.execute("UPDATE OR IGNORE sub_probe SET path=? WHERE path=?", (dst, m["old"]))
                mkvtag.ensure_table(c)
                c.execute("UPDATE mkv_meta SET path=? WHERE file_id=? AND path=?", (dst, m["file_id"], m["old"]))
                files_done += 1
        if done_pairs:
            c.commit()
            root = (folder_mv or moves[0])["root"]
            try:
                with open(os.path.join(root, "_filmtagger_renames.csv"), "a", newline="", encoding="utf-8-sig") as fh:
                    csv.writer(fh).writerows(done_pairs)
            except OSError:
                pass
            # drop an empty leftover folder (never the library root)
            if not folder_mv:
                for m in moves:
                    if m["kind"] != "file": continue
                    dd = os.path.dirname(m["old"])
                    try:
                        if (not os.listdir(dd) and os.path.normcase(dd) != os.path.normcase(m["root"])):
                            os.rmdir(dd)
                    except OSError:
                        pass
        return files_done, None
    except OSError as e:
        # Roll back a folder rename if later file/sidecar steps failed
        if folder_mv and done_pairs and os.path.normcase(os.path.abspath(done_pairs[0][0])) == os.path.normcase(os.path.abspath(folder_mv["old"])):
            try:
                if os.path.exists(folder_mv["new"]) and not os.path.exists(folder_mv["old"]):
                    os.rename(folder_mv["new"], folder_mv["old"])
            except OSError:
                pass
        return 0, str(e)

def rename_movie_file(c, fid, title=None, year=None):
    """Move a movie's folder, video file(s), and same-name sidecars (including .en.srt / .forced.srt) to
    the standard 'Title (Year)' name. Returns (renamed count, note, mkv result). Never overwrites."""
    moves, note = plan_movie_rename(c, fid, title=title, year=year)
    if note: return 0, note, None
    n, err = do_movie_moves(c, moves) if moves else (0, None)
    if err: return n, err, None
    mkv = movie_mkv_after(c, fid)
    return n, None, mkv

def after_movie_apply(c, fid):
    """After a committed TMDB/IMDb apply: rename to Title (Year), then write the MKV title if needed."""
    try:
        _, note, mkv = rename_movie_file(c, fid)
        return note, mkv
    except OSError as e:
        return f"Matched, but the file could not be renamed: {e}", movie_mkv_after(c, fid)


# ---------- MKV container titles (mkvtag.py, optional MKVToolNix): hard-matched scenes + regular movie .mkv ----------
MKV_SQL = ("SELECT x.id file_id, x.path, f.id, f.title, f.studio, f.release_date, f.cast_, COALESCE(f.notes,'') notes, "
           "COALESCE(f.tpdb_id,'') tid FROM files x JOIN films f ON f.id=x.film_id WHERE f.kind='scene' AND LOWER(x.path) LIKE '%.mkv'")
MOVIE_MKV_SQL = ("SELECT x.id file_id, x.path, f.id, f.title, f.year, COALESCE(f.kind,'') kind, COALESCE(f.primary_genre,'') pg "
                 "FROM files x JOIN films f ON f.id=x.film_id WHERE COALESCE(f.kind,'')<>'scene' AND LOWER(x.path) LIKE '%.mkv' "
                 "AND COALESCE(f.primary_genre,'') NOT IN ('Hardcore','Softcore')")

def mkv_plan(c, fid=None):
    """Hard-matched scene MKVs -> [dict(row, status, why, d, sig)]. status: 'update' | 'done' | 'skip'.
    Hard matched = ThePornDB id, studio/date/title present, file at its standard name (not a soft match), not Review by
    hand, not [Hold rename]. Only DB + file existence are checked here (no file is opened)."""
    mkvtag.ensure_table(c)
    rows = c.execute(MKV_SQL + (" AND f.id=?" if fid else "") + " ORDER BY x.id", ((fid,) if fid else ())).fetchall()
    if not rows: return []
    soft = soft_match_ids(c, sorted({r["id"] for r in rows}))
    done = {r[0]: r for r in c.execute("SELECT file_id, path, sig, size, title FROM mkv_meta")}
    out = []
    for r in rows:
        e = dict(file_id=r["file_id"], path=r["path"], film_id=r["id"], status="skip", why=None, d=None, sig=None)
        nl = r["notes"].lower()
        if "review by hand" in nl: e["why"] = "review"
        elif not r["tid"].strip(): e["why"] = "not_matched"
        elif HOLD_RENAME.lower() in nl or not (r["studio"] and r["release_date"] and r["title"]) or r["id"] in soft: e["why"] = "not_renamed"
        elif not os.path.isfile(r["path"]): e["why"] = "missing"
        else:
            e["d"] = mkvtag.desired(r); e["sig"] = mkvtag.signature(e["d"])
            m = done.get(r["file_id"])
            try: size = os.path.getsize(r["path"])
            except OSError: size = None
            same = m and m["sig"] == e["sig"] and os.path.normcase(m["path"] or "") == os.path.normcase(r["path"]) and m["size"] == size
            e["status"] = "done" if same else "update"; e["last_title"] = m["title"] if m else None
            e["known"] = bool(m) and os.path.normcase(m["path"] or "") == os.path.normcase(r["path"])  # written by Film Tagger before
        out.append(e)
    return out

def mkv_write_one(c, e):
    """Write one planned entry. -> ('updated'|'skipped'|'failed', message). Never raises."""
    try:
        cur = mkvtag.read_title(e["path"])
        if e.get("known") and e.get("last_title") is not None and cur is not None and cur != e["last_title"]:
            mkvtag.note(f"skipped (title edited by hand in the file) film {e['film_id']} file {e['file_id']}")
            return "skipped", "MKV title was edited by hand; left as is"
        if cur is not None and e.get("d") and cur == e["d"]["title"]:
            size = os.path.getsize(e["path"])
            c.execute("INSERT OR REPLACE INTO mkv_meta(file_id, path, sig, size, written_at, title) VALUES(?,?,?,?,?,?)",
                      (e["file_id"], e["path"], e["sig"], size, time.time(), e["d"]["title"])); c.commit()
            return "skipped", "MKV title already matches"
        ok_, msg = mkvtag.write(e["path"], e["d"])
        if not ok_:
            mkvtag.note(f"{'skipped' if 'in use' in msg else 'failed'}: film {e['film_id']} file {e['file_id']}: {msg}")
            return ("skipped" if "in use" in msg else "failed"), msg
        size = os.path.getsize(e["path"])
        c.execute("INSERT OR REPLACE INTO mkv_meta(file_id, path, sig, size, written_at, title) VALUES(?,?,?,?,?,?)",
                  (e["file_id"], e["path"], e["sig"], size, time.time(), e["d"]["title"]))
        c.execute("UPDATE files SET size=?, oshash=? WHERE id=?", (size, db.file_hash(e["path"]), e["file_id"]))  # header edit changes both
        c.commit(); mkvtag.note(f"updated film {e['film_id']} file {e['file_id']}")
        return "updated", "MKV title updated"
    except Exception as ex:
        try: mkvtag.note(f"failed: film {e.get('film_id')} file {e.get('file_id')}: {type(ex).__name__}: {ex}")
        except Exception: pass
        return "failed", f"{type(ex).__name__}: {ex}"

def mkv_after_rename(c, fid):
    """After a scene is hard matched/renamed: write its MKV title/tags. -> None (nothing to do) or dict(status, note).
    Never raises: a failure is logged and returned as a note (the match itself always stands)."""
    try:
        if not mkvtag.available(): return None
        plan = [e for e in mkv_plan(c, fid) if e["status"] == "update"]
        res = [mkv_write_one(c, e) for e in plan]
        if not res: return None
        bad = [m for s, m in res if s != "updated"]
        return dict(status="failed" if any(s == "failed" for s, _ in res) else ("skipped" if bad else "updated"),
                    note=("MKV title not written: " + bad[0]) if bad else "MKV title updated")
    except Exception as ex:
        try: mkvtag.note(f"failed after rename: film {fid}: {type(ex).__name__}: {ex}")
        except Exception: pass
        return dict(status="failed", note=f"MKV title not written: {type(ex).__name__}")


def movie_mkv_ok(c, fid):
    """True if this film is a regular (non-adult) movie eligible for MKV title writing."""
    r = c.execute("SELECT COALESCE(kind,'') kind, COALESCE(primary_genre,'') pg FROM films WHERE id=?", (fid,)).fetchone()
    if not r or r["kind"] == "scene" or r["pg"] in ("Hardcore", "Softcore"): return False
    try:
        import people as pm
        if fid in pm.adult_film_ids(c): return False
    except Exception: pass
    return True

def movie_mkv_plan(c, fid=None):
    """Regular-movie .mkv files -> [dict(...)] status 'update'|'done'|'skip'. Skip adult / missing; done when title already matches."""
    mkvtag.ensure_table(c)
    rows = c.execute(MOVIE_MKV_SQL + (" AND f.id=?" if fid else "") + " ORDER BY x.id", ((fid,) if fid else ())).fetchall()
    if not rows: return []
    try:
        import people as pm
        adult = pm.adult_film_ids(c)
    except Exception: adult = set()
    done = {r[0]: r for r in c.execute("SELECT file_id, path, sig, size, title FROM mkv_meta")}
    out = []
    for r in rows:
        e = dict(file_id=r["file_id"], path=r["path"], film_id=r["id"], status="skip", why=None, d=None, sig=None, kind="movie")
        if r["id"] in adult: e["why"] = "adult"
        elif not (r["title"] or "").strip(): e["why"] = "no_title"
        elif not os.path.isfile(r["path"]): e["why"] = "missing"
        else:
            e["d"] = mkvtag.desired_movie(r); e["sig"] = mkvtag.signature(e["d"])
            m = done.get(r["file_id"])
            try: size = os.path.getsize(r["path"])
            except OSError: size = None
            same = m and m["sig"] == e["sig"] and os.path.normcase(m["path"] or "") == os.path.normcase(r["path"]) and m["size"] == size
            if not same:
                cur = mkvtag.read_title(r["path"])
                if cur is not None and cur == e["d"]["title"]: same = True  # already matches; write path will record it
            e["status"] = "done" if same else "update"; e["last_title"] = m["title"] if m else None
            e["known"] = bool(m) and os.path.normcase(m["path"] or "") == os.path.normcase(r["path"])
        out.append(e)
    return out

def movie_mkv_after(c, fid):
    """Write MKV titles for a movie after title/year save or TMDB fetch. -> None or dict(status, note). Never raises."""
    try:
        if not mkvtag.available() or not movie_mkv_ok(c, fid): return None
        plan = [e for e in movie_mkv_plan(c, fid) if e["status"] == "update"]
        res = [mkv_write_one(c, e) for e in plan]
        if not res: return None
        bad = [m for s, m in res if s != "updated"]
        return dict(status="failed" if any(s == "failed" for s, _ in res) else ("skipped" if bad else "updated"),
                    note=("MKV title not written: " + bad[0]) if bad else "MKV title updated")
    except Exception as ex:
        try: mkvtag.note(f"failed movie mkv film {fid}: {type(ex).__name__}: {ex}")
        except Exception: pass
        return dict(status="failed", note=f"MKV title not written: {type(ex).__name__}")

def movie_mkv_counts(plan):
    k = {}
    for e in plan: k[e["why"] or e["status"]] = k.get(e["why"] or e["status"], 0) + 1
    elig = sum(1 for e in plan if e["status"] in ("update", "done"))
    return dict(available=mkvtag.available(), eligible=elig, would_update=k.get("update", 0), already=k.get("done", 0),
                skipped=dict(adult=k.get("adult", 0), no_title=k.get("no_title", 0), missing=k.get("missing", 0)), movie_mkvs=len(plan))

def mkv_counts(plan):
    k = {}
    for e in plan: k[e["why"] or e["status"]] = k.get(e["why"] or e["status"], 0) + 1
    elig = sum(1 for e in plan if e["status"] in ("update", "done"))
    return dict(available=mkvtag.available(), eligible=elig, would_update=k.get("update", 0), already=k.get("done", 0),
                skipped=dict(review=k.get("review", 0), not_matched=k.get("not_matched", 0), not_renamed=k.get("not_renamed", 0),
                             missing=k.get("missing", 0)), scene_mkvs=len(plan))

MKVJOB = {"running": False}
MKV_LOCK = threading.Lock()

@app.get("/api/mkv/titles/preview")
def mkv_titles_preview():
    """Dry run of Fix MKV titles: counts only, nothing is written."""
    c = C()
    try: return jsonify(dict(mkv_counts(mkv_plan(c)), tool=bool(mkvtag.find_tool("mkvpropedit"))))
    finally: c.close()

def run_mkv_job(force=False, ids=None):
    c = C()
    try:
        plan = [e for e in mkv_plan(c) if (e["status"] == "update" or (force and e["status"] == "done")) and (not ids or e["film_id"] in ids)]
        MKVJOB.update(total=len(plan))
        for e in plan:
            if MKVJOB.get("cancel"): break
            s, m = mkv_write_one(c, e); MKVJOB[s] = MKVJOB.get(s, 0) + 1
            if s == "failed": MKVJOB["last_error"] = m
            MKVJOB["done"] = MKVJOB.get("done", 0) + 1
    except Exception as ex: MKVJOB["error"] = f"{type(ex).__name__}: {ex}"
    finally:
        MKVJOB.update(running=False, finished_at=time.time()); c.close()
        mkvtag.note(f"Fix MKV titles finished: {json.dumps({k: MKVJOB.get(k, 0) for k in ('total', 'updated', 'skipped', 'failed')})}")

@app.post("/api/mkv/titles/start")
def mkv_titles_start():
    """Fix MKV titles for already hard-matched scenes (background). {force?: rewrite ones already written, film_ids?: only these}."""
    if not mkvtag.available(): return jsonify(error="MKVToolNix (mkvpropedit) is not installed: get it from mkvtoolnix.download"), 400
    d = request.get_json(silent=True) or {}
    with MKV_LOCK:
        if MKVJOB.get("running"): return jsonify(error="Fix MKV titles is already running"), 409
        MKVJOB.clear(); MKVJOB.update(running=True, done=0, total=0, updated=0, skipped=0, failed=0, started_at=time.time(), cancel=False)
    ids = {int(x) for x in d.get("film_ids") or []}
    threading.Thread(target=run_mkv_job, kwargs=dict(force=bool(d.get("force")), ids=ids or None), daemon=True).start()
    return jsonify(ok=True)

@app.get("/api/mkv/titles/status")
def mkv_titles_status():
    return jsonify({k: v for k, v in MKVJOB.items() if k != "cancel"})

@app.get("/api/mkv/movie-titles/preview")
def movie_mkv_titles_preview():
    """Dry run of Write MKV titles for movies: counts only."""
    c = C()
    try: return jsonify(dict(movie_mkv_counts(movie_mkv_plan(c)), tool=bool(mkvtag.find_tool("mkvpropedit"))))
    finally: c.close()

MOVIE_MKVJOB = {"running": False}
MOVIE_MKV_LOCK = threading.Lock()

def run_movie_mkv_job(force=False, ids=None):
    c = C()
    try:
        plan = [e for e in movie_mkv_plan(c) if (e["status"] == "update" or (force and e["status"] == "done")) and (not ids or e["film_id"] in ids)]
        MOVIE_MKVJOB.update(total=len(plan))
        for e in plan:
            if MOVIE_MKVJOB.get("cancel"): break
            s, m = mkv_write_one(c, e); MOVIE_MKVJOB[s] = MOVIE_MKVJOB.get(s, 0) + 1
            if s == "failed": MOVIE_MKVJOB["last_error"] = m
            MOVIE_MKVJOB["done"] = MOVIE_MKVJOB.get("done", 0) + 1
    except Exception as ex: MOVIE_MKVJOB["error"] = f"{type(ex).__name__}: {ex}"
    finally:
        MOVIE_MKVJOB.update(running=False, finished_at=time.time()); c.close()
        mkvtag.note(f"Write MKV titles for movies finished: {json.dumps({k: MOVIE_MKVJOB.get(k, 0) for k in ('total', 'updated', 'skipped', 'failed')})}")

@app.post("/api/mkv/movie-titles/start")
def movie_mkv_titles_start():
    """Write MKV titles for regular movies (background). {force?, film_ids?}."""
    if not mkvtag.available(): return jsonify(error="MKVToolNix (mkvpropedit) is not installed: get it from mkvtoolnix.download"), 400
    d = request.get_json(silent=True) or {}
    with MOVIE_MKV_LOCK:
        if MOVIE_MKVJOB.get("running"): return jsonify(error="Write MKV titles for movies is already running"), 409
        MOVIE_MKVJOB.clear(); MOVIE_MKVJOB.update(running=True, done=0, total=0, updated=0, skipped=0, failed=0, started_at=time.time(), cancel=False)
    ids = {int(x) for x in d.get("film_ids") or []}
    threading.Thread(target=run_movie_mkv_job, kwargs=dict(force=bool(d.get("force")), ids=ids or None), daemon=True).start()
    return jsonify(ok=True)

@app.get("/api/mkv/movie-titles/status")
def movie_mkv_titles_status():
    return jsonify({k: v for k, v in MOVIE_MKVJOB.items() if k != "cancel"})

@app.post("/api/films/<int:fid>/mkv/title")
def film_mkv_title(fid):
    """Write the MKV container title for one regular movie (or hard-matched scene)."""
    c = C()
    if not mkvtag.available(): return jsonify(error="MKVToolNix (mkvpropedit) is not installed: get it from mkvtoolnix.download"), 400
    f = c.execute("SELECT id, kind FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return jsonify(error="not found"), 404
    if f["kind"] == "scene":
        plan = [e for e in mkv_plan(c, fid) if e["status"] in ("update", "done")]
        if not plan: return jsonify(error="No eligible hard-matched .mkv for this scene"), 400
        if all(e["status"] == "done" for e in plan): return jsonify(ok=True, status="done", note="MKV title already matches")
        res = [mkv_write_one(c, e) for e in plan if e["status"] == "update"]
    else:
        if not movie_mkv_ok(c, fid): return jsonify(error="Only regular (non-adult) movies get an MKV title this way"), 400
        plan = [e for e in movie_mkv_plan(c, fid) if e["status"] in ("update", "done")]
        if not plan: return jsonify(error="No .mkv file for this movie"), 400
        if all(e["status"] == "done" for e in plan): return jsonify(ok=True, status="done", note="MKV title already matches")
        res = [mkv_write_one(c, e) for e in plan if e["status"] == "update"]
    bad = [m for st, m in res if st != "updated"]
    st = "failed" if any(x == "failed" for x, _ in res) else ("skipped" if bad else "updated")
    return jsonify(ok=st == "updated", status=st, note=("MKV title not written: " + bad[0]) if bad else "MKV title updated"), (200 if st != "failed" else 500)


def rename_waiting(c, fid):
    """Planned moves for a scene that is waiting for a by-hand rename, or [] (held, review, missing fields, already standard)."""
    if rename_held(c, fid) or is_review(c, fid): return []
    moves, note = plan_scene_rename(c, fid)
    return [] if note else [m for m in moves if m["new"] != m["old"]]

@app.get("/api/scenes/rename-waiting")
def scenes_rename_waiting():
    """Scenes whose file was not renamed automatically (flagged by the scan) plus older matched scenes (tpdb_id set) whose
    file name differs from the standard name. Preview only: nothing is moved. Review-by-hand scenes are never listed."""
    c = C(); items = []
    for r in c.execute("SELECT id, COALESCE(rename_pending,0) p FROM films WHERE kind='scene' AND (rename_pending=1 OR tpdb_id IS NOT NULL)"
                       " ORDER BY studio COLLATE FOLD, title COLLATE FOLD").fetchall():
        ms = rename_waiting(c, r[0])
        if not ms: continue
        f = c.execute("SELECT title, studio, release_date, cast_, custom_poster, poster_wide, poster FROM films WHERE id=?", (r[0],)).fetchone()
        for m in ms:  # rename_waiting already excludes Review-by-hand scenes, so a thumbnail is fine here
            items.append(dict(id=r[0], old=m["old"], new=m["new"], old_name=os.path.basename(m["old"]),
                              new_name=os.path.basename(m["new"]), flagged=bool(r[1]), title=f["title"], studio=f["studio"],
                              date=f["release_date"], cast=f["cast_"], thumb=f["custom_poster"] or f["poster_wide"] or f["poster"] or None))
    return jsonify(count=len(items), scenes=len({x["id"] for x in items}), flagged=sum(1 for x in items if x["flagged"]), items=items)

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

_GAL_BG, _GAL_BG_LOCK = set(), threading.Lock()


# ---------- Metacritic scores (mainstream Movies only; never Porn Scenes or Classic Porn) ----------
MC_CACHE_SEC = 30 * 24 * 3600
MC_MISS_CACHE_SEC = 24 * 3600  # a "not found" is retried after a day, not locked for 30
MC_MATCH_VER = 2  # bump when search/matching rules change so cached misses are retried on open
MC_CLEAR = "mc_slug=NULL, mc_metascore=NULL, mc_critic_count=NULL, mc_user_score=NULL, mc_user_count=NULL, mc_fetched_at=NULL, mc_match_ver=NULL"
_MC_BG, _MC_BG_LOCK, _MC_ERR_UNTIL = set(), threading.Lock(), {}

def mc_is_fresh(fetched_at, ttl=MC_CACHE_SEC, force=False):
    if force or not fetched_at: return False
    try: return (time.time() - float(fetched_at)) < ttl
    except (TypeError, ValueError): return False

def mc_is_miss(row):
    """Cached lookup that found no Metascore and no User score."""
    if not row or not row["mc_fetched_at"]: return False
    return row["mc_metascore"] is None and row["mc_user_score"] is None

def mc_needs_fetch(row, force=False):
    """True when this film should be looked up now (never fetched, stale, or a miss that should be retried)."""
    if force or not row: return True
    if not row["mc_fetched_at"]: return True
    if mc_is_miss(row):
        ver = row["mc_match_ver"] if "mc_match_ver" in row.keys() else None
        if ver != MC_MATCH_VER: return True  # matcher changed, or an old cache from before versions
        return not mc_is_fresh(row["mc_fetched_at"], ttl=MC_MISS_CACHE_SEC)
    return not mc_is_fresh(row["mc_fetched_at"], ttl=MC_CACHE_SEC)

def invalidate_metacritic_for_year(c, fid):
    """Drop cached Metacritic scores so a year change is looked up again. Keeps a slug the user set by hand."""
    if not db.is_metacritic_film(c, fid):
        return False
    ed = set(json.loads((c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] or "[]"))
    if "mc_slug" in ed:
        c.execute("UPDATE films SET mc_metascore=NULL, mc_critic_count=NULL, mc_user_score=NULL, mc_user_count=NULL, mc_fetched_at=NULL, mc_match_ver=NULL WHERE id=?", (fid,))
    else:
        c.execute("UPDATE films SET mc_slug=NULL, mc_metascore=NULL, mc_critic_count=NULL, mc_user_score=NULL, mc_user_count=NULL, mc_fetched_at=NULL, mc_match_ver=NULL WHERE id=?", (fid,))
    return True

def kick_metacritic(fid, force=False):
    """Start a background Metacritic lookup if this film is eligible and the cache is stale.
    Returns True if a fetch was started (or is already in flight), so the UI can poll."""
    fid = int(fid)
    c = C(); row = c.execute("SELECT id, kind, title, year, mc_slug, mc_metascore, mc_user_score, mc_fetched_at, mc_match_ver, edited_fields FROM films WHERE id=?", (fid,)).fetchone()
    if not row or not db.is_metacritic_film(c, fid): return False
    if not mc_needs_fetch(row, force): return False
    now = time.monotonic()
    if not force and _MC_ERR_UNTIL.get(fid, 0) > now: return False
    with _MC_BG_LOCK:
        if fid in _MC_BG: return True
        _MC_BG.add(fid)
    def work():
        try:
            refresh_metacritic(C(), fid, force=force)
        except Exception:
            _MC_ERR_UNTIL[fid] = time.monotonic() + 600  # 10 min cooldown after an unexpected error
        finally:
            with _MC_BG_LOCK: _MC_BG.discard(fid)
    threading.Thread(target=work, daemon=True).start()
    return True

def refresh_metacritic(c, fid, force=False):
    """Look up and store Metacritic scores for one film. Never raises to the caller. Returns a result dict."""
    row = c.execute("SELECT title, year, mc_slug, mc_metascore, mc_user_score, mc_fetched_at, mc_match_ver, edited_fields, kind FROM films WHERE id=?", (fid,)).fetchone()
    if not row: return {"ok": False, "reason": "not_found"}
    if not db.is_metacritic_film(c, fid): return {"ok": False, "reason": "skipped"}
    if not mc_needs_fetch(row, force):
        return {"ok": True, "cached": True, "slug": row["mc_slug"], "metascore": None}
    manual = "mc_slug" in set(json.loads(row["edited_fields"] or "[]"))
    slug = (row["mc_slug"] or "").strip() or None if manual or row["mc_slug"] else None
    # A hand-set slug is always used; an auto slug is reused until title/year changes (cleared in update_film).
    if not manual: slug = (row["mc_slug"] or "").strip() or None
    title, year = (row["title"] or "").strip(), row["year"]
    if not slug and not title:  # nothing to search with
        c.execute("UPDATE films SET mc_fetched_at=?, mc_match_ver=? WHERE id=?", (time.time(), MC_MATCH_VER, fid)); c.commit()
        return {"ok": False, "reason": "not_found"}
    result = mcmod.lookup(title, year, slug=slug)
    if result.get("reason") == "error":
        _MC_ERR_UNTIL[fid] = time.monotonic() + 600
        return result
    now = time.time()
    if result.get("ok"):
        new_slug = result.get("slug") or slug
        if manual and slug: new_slug = slug  # never overwrite a hand-set slug
        c.execute("UPDATE films SET mc_slug=?, mc_metascore=?, mc_critic_count=?, mc_user_score=?, mc_user_count=?, mc_fetched_at=?, mc_match_ver=? WHERE id=?",
                  (new_slug, result.get("metascore"), result.get("critic_count"), result.get("user_score"), result.get("user_count"), now, MC_MATCH_VER, fid))
        c.commit(); return result
    # not found: remember the miss (and keep a hand-set slug so the user can see what was tried)
    keep_slug = slug if manual else (result.get("slug") if result.get("slug") and slug else None)
    # fetched_at + current match version: retried after a day, or sooner if MC_MATCH_VER is bumped
    c.execute("UPDATE films SET mc_slug=?, mc_metascore=NULL, mc_critic_count=NULL, mc_user_score=NULL, mc_user_count=NULL, mc_fetched_at=?, mc_match_ver=? WHERE id=?",
              (keep_slug, now, MC_MATCH_VER, fid))
    c.commit(); return result

def _mc_eligible_ids(c, retry=False, retry_misses=False, film_ids=None, limit=None):
    """Mainstream movies that still need a lookup. retry also includes stale (30-day) rows.
    retry_misses: films that were looked up but got no scores (re-check after a matcher change)."""
    where = "WHERE " + db.MC_ELIGIBLE_SQL
    args = []
    if film_ids:
        ids = [int(i) for i in film_ids]
        where += f" AND id IN ({','.join('?' * len(ids))})"
        args.extend(ids)
    if retry_misses:
        where += " AND mc_fetched_at IS NOT NULL AND mc_metascore IS NULL AND mc_user_score IS NULL"
    elif retry:
        where += " AND (mc_fetched_at IS NULL OR mc_fetched_at < ?)"
        args.append(time.time() - MC_CACHE_SEC)
    else:
        where += " AND mc_fetched_at IS NULL"
    sql = "SELECT id FROM films " + where + " ORDER BY title COLLATE FOLD, id"
    if limit: sql += " LIMIT ?"; args.append(int(limit))
    return [r[0] for r in c.execute(sql, args)]

def run_metacritic_job(ids):
    c = C()
    try:
        for fid in ids:
            if JOB["cancel"]: break
            try:
                r = refresh_metacritic(c, fid, force=False)
                if r.get("ok") and not r.get("cached"): JOB["matched"] += 1
                else: JOB["skipped"] += 1
            except Exception as e:
                JOB["errors"] += 1; JOB["error"] = str(e)
                if JOB["errors"] >= 5 and JOB["matched"] == 0:
                    JOB["error"] += " (stopped after repeated failures)"; break
            JOB["done"] += 1
    finally:
        JOB["running"] = False; JOB["finished_at"] = time.time(); c.close()

@app.post("/api/metacritic/start")
def metacritic_start():
    """Backfill Metacritic scores for mainstream Movies only (skips Porn Scenes and Classic Porn).
    Body: {retry: bool, limit: int, film_ids: [..]}. Default is films never fetched. Does not start a
    second job if one is already running."""
    d = request.get_json(silent=True) or {}
    c = C()
    with JOB_LOCK:
        if JOB.get("running"): return jsonify(error="Another job is already running"), 409
        ids = _mc_eligible_ids(c, retry=bool(d.get("retry") or d.get("retry_checked")), retry_misses=bool(d.get("retry_misses")), film_ids=d.get("film_ids"), limit=d.get("limit"))
        JOB.clear(); JOB.update(running=True, kind="metacritic", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False, started_at=time.time())
        threading.Thread(target=run_metacritic_job, args=(ids,), daemon=True).start()
    return jsonify(JOB)

@app.post("/api/films/<int:fid>/metacritic")
def film_metacritic(fid):
    """Fetch (or refresh) Metacritic scores for one mainstream movie. Scenes and Classic Porn are skipped."""
    c = C()
    if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone(): return jsonify(error="not found"), 404
    if not db.is_metacritic_film(c, fid):
        return jsonify(ok=False, skipped=True, reason="not a mainstream movie")
    refresh_metacritic(c, fid, force=True)
    return get_film(fid)

@app.get("/api/imdb-years/status")
def imdb_years_status():
    return jsonify(imdbmod.status())

@app.post("/api/imdb-years/refresh")
def imdb_years_refresh():
    """Start a background download/rebuild of title.basics. Body: {force: bool}. Soft-fails; does not block."""
    d = request.get_json(silent=True) or {}
    started = imdbmod.kick_refresh(force=bool(d.get("force", True)))
    st = imdbmod.status()
    st["started"] = started
    return jsonify(st)

@app.post("/api/imdb-years/cancel")
def imdb_years_cancel():
    imdbmod.STATE["cancel"] = True
    return jsonify(ok=True)

@app.get("/api/imdb-years/mismatches")
def imdb_years_mismatches():
    """Year fixes (stored IMDb id, matching title, year ±1) and suspected wrong IMDb links. Read-only."""
    st = imdbmod.status()
    if not st.get("ready"):
        return jsonify(mismatches=[], wrong_links=[], count=0, wrong_link_count=0, by_library={},
                       wrong_links_by_library={}, status=st,
                       error=st.get("error") or "IMDb year data is not ready yet")
    d = imdbmod.classify_films(C())
    fixes, wrong = d["year_fixes"], d["wrong_links"]
    return jsonify(mismatches=fixes, wrong_links=wrong, count=len(fixes), wrong_link_count=len(wrong),
                   by_library=imdbmod.counts_by_library(fixes),
                   wrong_links_by_library=imdbmod.counts_by_library(wrong), status=st)

def _imdb_ids_from_body():
    d = request.get_json(silent=True) or {}
    ids = d.get("film_ids") or d.get("ids") or []
    try:
        return [int(x) for x in ids], None
    except (TypeError, ValueError):
        return None, (jsonify(error="film_ids must be a list of ids"), 400)

def preview_imdb_year_renames(c, ids):
    """Plan year + file/folder/sidecar renames for selected films. Does not write films.db or move files."""
    st = imdbmod.status()
    items = []
    if not st.get("ready"):
        return dict(ok=False, error="IMDb year data is not ready yet", status=st, items=[], count=0)
    # Reuse mismatch resolution so preview agrees with the list
    want = set(ids)
    rows = {r["film_id"]: r for r in imdbmod.find_mismatches(c) if r["film_id"] in want}
    for fid in ids:
        m = rows.get(fid)
        if not m:
            # may already match, or unresolved
            film = c.execute("SELECT id, title, year, kind FROM films WHERE id=?", (fid,)).fetchone()
            if not film:
                items.append(dict(film_id=fid, ok=False, reason="not found", moves=[], title="", year=None, imdb_year=None))
                continue
            items.append(dict(film_id=fid, ok=False, reason="No IMDb year mismatch for this film", moves=[],
                              title=film["title"] or "", year=film["year"], imdb_year=None))
            continue
        item = dict(film_id=fid, title=m["title"], year=m["year"], imdb_year=m["imdb_year"],
                    imdb_id=m["imdb_id"], library=m.get("library"), hand_edited=m.get("hand_edited"),
                    ok=True, reason=None, moves=[])
        if (c.execute("SELECT kind FROM films WHERE id=?", (fid,)).fetchone() or [None])[0] == "scene":
            item.update(ok=True, reason=None, moves=[])  # scene names do not use the year in Title (Year) form
            items.append(item); continue
        moves, err = plan_movie_rename(c, fid, title=m["title"], year=m["imdb_year"])
        item["moves"] = [dict(kind=x["kind"], old=x["old"], new=x["new"]) for x in (moves or [])]
        if err:
            item["ok"] = False
            item["reason"] = err
        items.append(item)
    return dict(ok=True, status=st, items=items, count=len(items),
                ready=sum(1 for x in items if x["ok"]), blocked=sum(1 for x in items if not x["ok"]))

@app.post("/api/imdb-years/preview")
def imdb_years_preview():
    """Dry-run: show current folder/file/sidecar names and the Title (Year) names after the IMDb year.
    Does not change films.db or rename anything."""
    ids, err = _imdb_ids_from_body()
    if err: return err
    if not ids:
        return jsonify(error="No films selected"), 400
    return jsonify(preview_imdb_year_renames(C(), ids))

@app.post("/api/imdb-years/apply")
def imdb_years_apply():
    """Apply IMDb years to the selected films. Renames each movie's folder, video(s) and sidecars
    (including language-tagged subtitles) first; if a rename fails, that film's year is left unchanged."""
    ids, err = _imdb_ids_from_body()
    if err: return err
    if not ids:
        return jsonify(error="No films selected"), 400
    st = imdbmod.status()
    if not st.get("ready"):
        return jsonify(error="IMDb year data is not ready yet", status=st), 409
    c = C()
    preview = preview_imdb_year_renames(c, ids)
    applied, skipped, changed, renamed, details = 0, 0, [], 0, []
    for item in preview["items"]:
        fid = item["film_id"]
        if not item.get("ok"):
            skipped += 1
            details.append(dict(film_id=fid, title=item.get("title") or "", ok=False, reason=item.get("reason") or "skipped", renamed=0))
            continue
        # Rename first (using the IMDb year), then write the year so a failed rename never desyncs names
        moves, plan_err = plan_movie_rename(c, fid, title=item.get("title"), year=item.get("imdb_year"))
        if plan_err:
            skipped += 1
            details.append(dict(film_id=fid, title=item.get("title") or "", ok=False, reason=plan_err, renamed=0))
            continue
        try:
            n, move_err = do_movie_moves(c, moves) if moves else (0, None)
        except OSError as e:
            n, move_err = 0, str(e)
        if move_err:
            skipped += 1
            details.append(dict(film_id=fid, title=item.get("title") or "", ok=False, reason=move_err, renamed=0))
            continue
        result = imdbmod.apply_fixes(c, [fid])
        if result["changed"]:
            for ch in result["changed"]:
                invalidate_metacritic_for_year(c, ch["film_id"])
            c.commit()
            for ch in result["changed"]:
                kick_metacritic(ch["film_id"], force=True)
                changed.append(ch)
            # MKV title with the new year (files are already at the new name)
            movie_mkv_after(c, fid)
        elif result["applied"]:
            c.commit()
        applied += result["applied"]
        skipped += result["skipped"]
        renamed += n
        details.append(dict(film_id=fid, title=item.get("title") or "", ok=True, reason=None, renamed=n,
                            year=item.get("year"), imdb_year=item.get("imdb_year"),
                            moves=[dict(kind=x["kind"], old=x["old"], new=x["new"]) for x in (moves or [])]))
    return jsonify(ok=True, applied=applied, skipped=skipped, changed=changed, renamed=renamed, details=details)

@app.post("/api/imdb-years/export")
def imdb_years_export():
    """Write the current mismatch list to a CSV. Default path: Desktop/Film Tagger stuff/imdb_year_mismatches.csv."""
    d = request.get_json(silent=True) or {}
    path = (d.get("path") or "").strip() or imdbmod.default_csv_path()
    st = imdbmod.status()
    if not st.get("ready"):
        return jsonify(error="IMDb year data is not ready yet", status=st), 409
    d = imdbmod.classify_films(C())
    rows = (d["year_fixes"] + d["wrong_links"])
    written = imdbmod.write_csv(path, rows)
    return jsonify(ok=True, path=written, count=len(d["year_fixes"]), wrong_link_count=len(d["wrong_links"]),
                   by_library=imdbmod.counts_by_library(d["year_fixes"]),
                   wrong_links_by_library=imdbmod.counts_by_library(d["wrong_links"]))

def kick_gallery(fid):
    """Refresh one film's gallery_path/count off the request thread (deduped while in flight)."""
    fid = int(fid)
    with _GAL_BG_LOCK:
        if fid in _GAL_BG: return
        _GAL_BG.add(fid)
    def work():
        try: scene_gallery(C(), fid)
        except Exception: pass
        finally:
            with _GAL_BG_LOCK: _GAL_BG.discard(fid)
    threading.Thread(target=work, daemon=True).start()

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

GAL_CACHE = {}  # film id -> (time, kind, folder): thumbnail/photo requests reuse the folder found by the listing
GAL_TTL = 120

def _gallery_or_404(fid, fresh=True):
    """fresh=False (thumb/photo/poster requests): reuse the folder found by a recent listing instead of re-scanning the
    gallery (hundreds of files on a USB drive) once per thumbnail. Review-by-hand is still checked every time."""
    c = C()
    if is_review(c, fid): return None, (jsonify(error="Gallery hidden: this scene is flagged for review by hand"), 403)
    hit = GAL_CACHE.get(fid)
    if not fresh and hit and time.time() - hit[0] < GAL_TTL and os.path.isdir(hit[2]): return (c, hit[1], hit[2]), None
    kind, folder = scene_gallery(c, fid)
    if folder: GAL_CACHE[fid] = (time.time(), kind, folder)
    else: GAL_CACHE.pop(fid, None)
    if not folder: return None, (jsonify(error="No gallery for this scene"), 404)
    return (c, kind, folder), None

@app.get("/api/films/<int:fid>/gallery")
def gallery_list(fid):
    g, err = _gallery_or_404(fid)
    if err: return err
    c, kind, folder = g; names = gallerymod.images(folder)
    return jsonify(kind=kind, folder=os.path.basename(folder), count=len(names), images=names, groups=gallerymod.groups(names))

@app.get("/api/films/<int:fid>/gallery/thumb")
def gallery_thumb(fid):
    g, err = _gallery_or_404(fid, fresh=False)
    if err: return err
    p = gallerymod.safe_name(g[2], request.args.get("name"))
    if not p: return jsonify(error="not found"), 404
    t = gallerymod.thumb(p, GALLERY_THUMBS)
    return send_file(t or p, max_age=86400 * 30)

@app.get("/api/films/<int:fid>/gallery/image")
def gallery_image(fid):
    g, err = _gallery_or_404(fid, fresh=False)
    if err: return err
    p = gallerymod.safe_name(g[2], request.args.get("name"))
    if not p: return jsonify(error="not found"), 404
    return send_file(p, max_age=86400)

@app.post("/api/films/<int:fid>/gallery/poster")
def gallery_poster(fid):
    """{name}: copy that gallery photo into the posters folder and use it as the custom poster (smart crop runs after)."""
    g, err = _gallery_or_404(fid, fresh=False)
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
    _set_custom_poster(c, fid, val); c.commit(); kick_focus()
    return jsonify(dict(get_film(fid).json, poster_note="from gallery", art_export=_art_export(c, fid)))

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
        # Site prefixes (StepSiblings) often miss threads posted under a network prefix (TeamSkeet). After prefix
        # steps fail, retry the same performers with no prefix. Leave the studio name out: vBulletin ANDs every
        # keyword, and the thread title usually has the network name, not the site.
        noprefix = [(one, None) for one in perfs[:2]] if prefix else []
        plain = [(default_q, None)]
        # last resort: the lead performer's last name (and each alias's last word) alone, only with a studio prefix
        lasts = []
        if prefix:
            for n in [perfs[0]] + alias_map[perfs[0]]:
                w = n.split()[-1] if len(n.split()) > 1 else ""
                if len(w) >= 3 and db.fold(w) not in {db.fold(x) for x in lasts}: lasts.append(w)
        last = [(w, prefix, False, "last") for w in lasts]
        alt = [(one, alt_prefix) for one in perfs[:1]] if alt_prefix else []  # the old prefix, after the learned one
        # aliases keep priority over the no-prefix fallback (room is computed without noprefix first)
        room = fgmod.MAX_STEPS - len(head) - len(alt) - len(plain) - len(last)
        mid = (lead_al + co_al)[:max(0, room)]  # co-star aliases are trimmed first, then the lead's
        left = fgmod.MAX_STEPS - len(head) - len(mid) - len(alt) - len(plain) - len(last)
        noprefix = noprefix[:max(0, left)]
        # learned: titles leave names out -> prefix + last names right after the full names, before aliases
        steps = head + last + mid + alt + noprefix + plain if no_names else head + mid + alt + noprefix + plain + last
        steps = steps[:fgmod.MAX_STEPS]
    steps = [tuple(x) + (False, "")[len(x) - 2:] for x in steps]
    steps = list(dict.fromkeys(steps))[:fgmod.MAX_STEPS]
    res, pref_used, t0, last_name = [], False, time.time(), False
    fgmod.SEARCH.update(started=t0, n=len(steps), step="")
    fgmod.SEARCH_CANCEL.clear()
    try:
        for k, (sq, sp, wide, kind) in enumerate(steps):
            if fgmod.search_cancelled(): raise fgmod.SearchCancelled()
            if k and time.time() - t0 > fgmod.SEARCH_BUDGET_S:
                tried.append(dict(query=sq, prefix=sp, n=0, skipped="time limit")); break
            fgmod.SEARCH["step"] = f"Step {k + 1} of {len(steps)}: “{sq}”" + (f" with prefix {sp}" if sp else "") + (" in all forums" if wide else "")
            res, cached = fgmod.search(sq, sp, block=k > 0, all_forums=wide)
            tried.append(dict(query=sq, prefix=sp, n=len(res), cached=cached, all_forums=wide, last_name=kind == "last")); pref_used = bool(sp)
            last_name = kind == "last"
            if res: break
    except fgmod.SearchCancelled:
        return jsonify(error="Search cancelled", cancelled=True, default_query=default_q, tried=tried), 499
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
    fgmod.prepare(); fgmod.DL.update(running=True, film_id=fid, files=0, error=None)
    threading.Thread(target=fgmod.run_download, args=(url, dest, done), kwargs=dict(fresh=False), daemon=True).start()
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

# ---------- Make stills: ~20 frames from the video as the title's gallery (stills.py), when Find Gallery finds nothing ----------
import stills as stillsmod

def _stills_target(fid):
    """-> (video path, dest folder, existing image count, None) or (.., error response)."""
    c = C(); f = c.execute("SELECT id FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return None, None, 0, (jsonify(error="not found"), 404)
    if is_review(c, fid): return None, None, 0, (jsonify(error="Blocked: this title is flagged for review by hand"), 403)
    path = next((p for (p,) in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (fid,)) if os.path.isfile(p)), None)
    if not path: return None, None, 0, (jsonify(error="The video file was not found"), 400)
    kind, cur = gallerymod.find(path, db.VIDEO_EXT)
    dest = cur if kind == "canonical" else gallerymod.canonical_dir(path)
    existing = len(gallerymod.images(dest)) if os.path.isdir(dest) else 0
    return path, dest, existing, None

@app.get("/api/films/<int:fid>/stills/info")
def stills_info(fid):
    """For the Make stills popup: how many images the target stills folder already holds, and the count choices."""
    _, dest, existing, err = _stills_target(fid)
    if err: return err
    return jsonify(existing=existing, dest=os.path.basename(dest), counts=list(stillsmod.COUNTS), default=stillsmod.COUNT)

@app.post("/api/films/<int:fid>/stills")
def make_stills(fid):
    """{count: 10|20|40, mode: 'add'|'replace'} (overwrite: true = add, older clients): films or scenes. Blocked like
    galleries for titles flagged 'Review by hand'. Replace moves the folder's current images into a hidden backup
    subfolder first (never deletes); Add numbers the new stills after the old ones."""
    path, dest, existing, err = _stills_target(fid)
    if err: return err
    if stillsmod.JOB.get("running"): return jsonify(error="Stills are already being made for another title"), 409
    b = request.json or {}
    try: count = int(b.get("count") or stillsmod.COUNT)
    except (TypeError, ValueError): count = 0
    if count not in stillsmod.COUNTS: return jsonify(error="count must be one of " + ", ".join(map(str, stillsmod.COUNTS))), 400
    mode = b.get("mode") or ("add" if b.get("overwrite") else None)
    if existing and mode not in ("add", "replace"):
        return jsonify(error=f"This title already has a gallery with {existing} photos", confirm=True, existing=existing), 409
    backup = None
    if existing and mode == "replace":
        try: backup, _ = stillsmod.backup_existing(dest, gallerymod.IMG_EXT)
        except OSError as e: return jsonify(error="Could not move the old stills aside: " + str(e)), 500
    def done(): GAL_CACHE.pop(fid, None); scene_gallery(C(), fid)
    stillsmod.start(fid, path, dest, done, count, stillsmod.next_number(dest))
    return jsonify(ok=True, dest=os.path.basename(dest), count=count, backup=backup)

@app.post("/api/films/<int:fid>/gallery/open")
def gallery_open(fid):
    """Open the title's gallery / stills folder in Explorer (blocked for Review by hand, like the gallery itself)."""
    g, err = _gallery_or_404(fid)
    if err: return err
    if os.name != "nt": return jsonify(error="Only available on Windows"), 400
    os.startfile(g[2])
    return jsonify(ok=True)

@app.get("/api/stills/status")
def stills_status():
    j = {k: v for k, v in stillsmod.JOB.items() if k != "dest"}; j["dest"] = os.path.basename(stillsmod.JOB.get("dest") or "")
    return jsonify(j)

@app.get("/api/findgallery/status")
def findgallery_status():
    j = {k: v for k, v in fgmod.DL.items() if k in ("running", "film_id", "url", "files", "error", "removed", "rc", "started_at", "finished_at", "last_msg", "cancel", "cancelled", "partial_removed", "expected", "failed")}
    j["dest"] = os.path.basename(fgmod.DL.get("dest") or "")
    st = fgmod.SEARCH
    j["search"] = dict(step=st["step"], secs=round(time.time() - st["started"]) if st["started"] else 0) if st["step"] else None
    return jsonify(j)

@app.post("/api/findgallery/cancel")
def findgallery_cancel():
    """{search?: bool, download?: bool} (both by default): stop the running search and/or gallery download."""
    d = request.get_json(silent=True) or {}
    if d.get("search", True): fgmod.cancel_search()
    if d.get("download", True) and fgmod.DL.get("running"): fgmod.cancel()
    return jsonify(ok=True)

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
                TP["current"] = os.path.basename(p[0]) if p else ""
                rl.wait(); r = tpdb.identify(token, p[0]) if p else None
                s = r["scene"] if r and r.get("confident") else None  # same as lookup(): details only on a confident match
                if s:
                    rl.wait(); s = tpdb.get_scene(token, s.get("id") or s.get("_id")) or s
                    apply_scene(c, fid, s); TP["matched"] += 1; c.commit()
                    if tpdb.rename_safe(r) and not rename_held(c, fid) and not is_review(c, fid):
                        try:
                            rn = rename_scene_file(c, fid)
                            if rn[0]: TP["renamed"] = TP.get("renamed", 0) + 1
                            if rn[2]: TP["mkv_" + rn[2]["status"]] = TP.get("mkv_" + rn[2]["status"], 0) + 1  # MKV title (never fatal)
                        except OSError as e: TP["rename_errors"] = TP.get("rename_errors", 0) + 1; TP["error"] = f"Rename failed: {e}"
                    if rename_waiting(c, fid):  # matched but the file still has a non-standard name: wait for a by-hand rename
                        c.execute("UPDATE films SET rename_pending=1 WHERE id=?", (fid,)); TP["rename_waiting"] = TP.get("rename_waiting", 0) + 1
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
        TP.clear(); TP.update(running=True, kind="tpdb", done=0, total=len(ids), matched=0, skipped=0, errors=0, error=None, cancel=False, current="")
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

@app.post("/api/tpdb/cancel")
def tpdb_cancel(): TP["cancel"] = True; return jsonify(TP)

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

# ---------- Reports (read-only) ----------
import reports as repmod

@app.get("/api/reports/options")
def reports_options():
    c = C(); o = repmod.options(c)
    o["ai_configured"] = bool(db.get_setting(c, "ai_url", "") and db.get_setting(c, "ai_model", ""))
    return jsonify(o)

@app.post("/api/reports/run")
def reports_run():
    return jsonify(repmod.run(C(), (request.json or {}).get("spec") or {}))

@app.post("/api/reports/films")
def reports_films():
    return jsonify(films=repmod.films_list(C(), (request.json or {}).get("ids") or []))

@app.get("/api/reports/synonyms")
def reports_synonyms_get():
    return jsonify(synonyms=repmod.synonyms(C()))

@app.put("/api/reports/synonyms")
def reports_synonyms_put():
    try: return jsonify(synonyms=repmod.set_synonyms(C(), (request.json or {}).get("synonyms")))
    except ValueError as e: return jsonify(error=str(e)), 400

@app.post("/api/reports/ask")
def reports_ask():
    c = C(); q = ((request.json or {}).get("question") or "").strip()
    if not q: return jsonify(error="Type a question first."), 400
    url, model, key = db.get_setting(c, "ai_url", ""), db.get_setting(c, "ai_model", ""), db.get_setting(c, "ai_key", "")
    if not (url and model):
        return jsonify(error="No AI service is set up. Add one in Library & Settings \u2192 Tags & Categories \u2192 Smart tagging, or use the ready-made reports."), 400
    try:
        res = repmod.ask(c, q, lambda p: ai_chat(url, key, model, p, timeout=90, temperature=0))  # AI writes a read-only SELECT; falls back to a report spec
    except urllib.error.HTTPError as e: return jsonify(error=f"AI service error HTTP {e.code}"), 502
    except Exception as e: return jsonify(error=f"Could not read the AI's answer: {e}"), 502
    if res.get("mode") == "sql":
        try: ask_charts(c, res)
        except Exception as e: res["chart_error"] = type(e).__name__
    return jsonify(res)

def _ids_for(c, f):
    where, args = build_query(f)
    return [r[0] for r in c.execute(f"SELECT f.id FROM films f {where}", args)]

def ask_charts(c, res):
    """Chart + optional per-year context chart for an Ask a question result (numbers only; the browser draws SVG)."""
    import askcharts as ac
    ch = ac.pick(res["columns"], res["rows"], res.get("chart_hint"), res.get("sql") or "", res.get("directors") or [])
    if ch and ch["type"] == "ring" and ch.get("part_filter") and ch.get("total_filter"):  # exact 'other' titles for the ring
        part = set(_ids_for(c, ch["part_filter"])); tot = _ids_for(c, ch["total_filter"])
        other = [i for i in tot if i not in part]
        if len(part) == (ch.get("part") or -1) and len(tot) == (ch.get("total") or -1):
            ch["other_ids"] = ",".join(map(str, other))
        else:  # the library filters don't reproduce the query's numbers: click through the query's own titles only
            ch["part_filter"] = ch["total_filter"] = None
    if ch:  # a click opens the same titles the bar counts: keep a library filter only if it reproduces the row's titles
        cells = [x for row in ch.get("cells", []) for x in row if x] + list(ch.get("items", []))
        for it in cells[:200]:
            if it.get("filter") and it.get("ids"):
                want = {int(x) for x in str(it["ids"]).split(",") if x.strip().isdigit()}
                if set(_ids_for(c, it["filter"])) != want: it["filter"] = None
    res["chart"] = ch
    cs = ac.context_spec(res.get("sql") or "", res.get("directors") or [], res.get("mentions_cast"))
    if cs:
        where, args = build_query(cs["filter"])
        cnt = {y: n for y, n in c.execute(f"SELECT f.year, COUNT(*) FROM films f {where} {'AND' if where else 'WHERE'} f.year>0 GROUP BY f.year", args)}
        if cnt:
            a, b = cs["range"]; lo, hi = min(min(cnt), a), max(max(cnt), b)
            if hi - lo <= 150:
                res["context"] = dict(title=ac.context_title(cs["filter"]), range=[a, b], filter=cs["filter"],
                                      inside=sum(n for y, n in cnt.items() if a <= y <= b), total=sum(cnt.values()),
                                      items=[dict(label=y, values=[cnt.get(y, 0)], filter=dict(cs["filter"], year_min=y, year_max=y)) for y in range(lo, hi + 1)])

    ids = res.get("count_ids")
    if ids is not None and len(res.get("rows") or []) == 1:  # 'how many ...': big number + those titles by year + the list
        res["chart"] = dict(type="stat", candidates=["stat"], hinted=False, filter=None, ids=",".join(map(str, ids)) or None,
                            tiles=[dict(label="matching titles", value=len(ids), pct=False)])
        rows = [dict(id=i, title=t, year=y) for i, t, y in c.execute(
            "SELECT id, title, year FROM films WHERE id IN (SELECT value FROM json_each(?)) ORDER BY year IS NULL, year, title", (json.dumps(ids),))]
        res["titles"] = rows[:300]; res["titles_total"] = len(rows)
        by = {}
        for x in rows:
            if x["year"]: by.setdefault(x["year"], []).append(x["id"])
        res.pop("context", None)
        if by and max(by) - min(by) <= 150:
            res["context"] = dict(title="Those titles by year", range=None, filter=None, inside=sum(map(len, by.values())), total=len(rows),
                                  items=[dict(label=y, values=[len(by.get(y, []))], ids=",".join(map(str, by.get(y, []))) or None, filter=None)
                                         for y in range(min(by), max(by) + 1)])

DASH = {}  # scope -> {"at", "day", "data"}; ~60 s per scope
EXPL = {}  # explore query -> (at, day, data)


def dash_movies(c):
    """(film count, library path) for the 'Movies' library: the configured root named Movies, counted the same
    way the library filter (?lib=) matches it. (0, None) when there is no such library."""
    sr = [os.path.normcase(x) for x in db.scene_roots(c)]
    lib = next((r.rstrip("\\/") for r in db.get_setting(c, "roots", []) or []
                if r and os.path.normcase(r.rstrip("\\/")) not in sr
                and _re.split(r"[\\/]", r.rstrip("\\/"))[-1].lower() == "movies"), None)
    if not lib: return 0, None
    likes, args = [], []
    for pre in db.lib_prefixes(c, lib):
        esc_ = pre.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        likes += ["LOWER(path) LIKE ? ESCAPE '\\'", "LOWER(path) LIKE ? ESCAPE '\\'"]; args += [esc_ + "\\\\%", esc_ + "/%"]
    n = c.execute("SELECT COUNT(*) FROM films f WHERE f.id IN (SELECT film_id FROM files WHERE " + " OR ".join(likes) + ")", args).fetchone()[0]
    return n, lib

DASH_SCOPES = ("films", "movies", "classic", "scenes")

def dash_scopes(c):
    """Dashboard scope tiles -> the library filters (front-end filter keys) they stand for. Movies only when a
    library folder named Movies exists."""
    out = dict(films={"special": "films_only"}, classic={"pgenre": repmod.ADULT_GENRE, "special": "films_only"},
               scenes={"special": "scenes"})
    n, lib = dash_movies(c)
    if lib: out["movies"] = {"lib": lib}
    return out

def fk_api(fk):
    """Front-end filter keys -> /api/films query params ('special' becomes its own flag, as params() does)."""
    q = {k: v for k, v in fk.items() if v not in (None, "") and k != "special"}
    if fk.get("special"): q[fk["special"]] = "1"
    return q

@app.get("/api/dashboard")
def dashboard():
    """Dashboard summary (read-only queries), cached ~60 s per scope; ?force=1 recomputes. ?scope=films|movies|classic|scenes
    limits everything except the scope tiles to that part of the library (the same filter as 'Show these titles')."""
    import datetime as _dt
    today = _dt.date.today()
    scope = request.args.get("scope") or ""
    if scope not in DASH_SCOPES: scope = ""
    e = DASH.get(scope)
    if not request.args.get("force") and e and e["day"] == today and time.time() - e["at"] < 60:
        return jsonify(dict(e["data"], cached=True))
    c = C()
    try:
        scopes = dash_scopes(c)
        if scope and scope not in scopes: scope = ""
        S = "1"
        if scope:
            where, args = build_query(fk_api(scopes[scope]))
            c.execute("CREATE TEMP TABLE IF NOT EXISTS dash_scope(id INTEGER PRIMARY KEY)"); c.execute("DELETE FROM temp.dash_scope")
            c.execute(f"INSERT INTO temp.dash_scope SELECT f.id FROM films f {where}", args)
            S = "id IN (SELECT id FROM temp.dash_scope)"
        d = repmod.dashboard(c, today, S)
        d["health"]["rename_waiting"] = len(soft_match_ids(c))  # same set as the Rename waiting view (whole library), from stored paths
        d["stats"]["movies"], d["stats"]["movies_lib"] = dash_movies(c)
        d["scope"], d["scopes"], d["scope_filter"] = scope, scopes, scopes.get(scope, {})
    finally: c.close()
    d["computed_at"] = time.time(); DASH[scope] = dict(at=time.time(), day=today, data=d)
    return jsonify(dict(d, cached=False))

@app.get("/api/dash/explore")
def dash_explore():
    """Drill-down aggregates for the Dashboard explore view. Takes the same filter params as /api/films (plus month=0-12
    and an optional scope=), so 'total' always equals the library list for the same filters. Read-only, cached ~60 s."""
    import datetime as _dt
    today = _dt.date.today()
    q = {k: request.args.get(k) for k in request.args if k not in ("force", "_", "sort", "order", "limit", "offset")}
    scope = q.pop("scope", "") or ""
    key = json.dumps([scope, sorted(q.items())])
    e = EXPL.get(key)
    if not request.args.get("force") and e and e[1] == today and time.time() - e[0] < 60:
        return jsonify(dict(e[2], cached=True))
    c = C()
    try:
        if scope:
            for k, v in fk_api(dash_scopes(c).get(scope, {})).items(): q.setdefault(k, v)
        where, args = build_query(q)
        d = repmod.explore(c, where, args)
    finally: c.close()
    if len(EXPL) > 300: EXPL.clear()
    EXPL[key] = (time.time(), today, d)
    return jsonify(dict(d, cached=False))

@app.get("/api/about")
def about():
    return jsonify(name="Film Tagger", version=APP_VERSION, author="Kyle R. Hudson",
                   description="A local catalogue and tagger for your film and scene library.",
                   repo_url=f"https://github.com/{REPO}", releases_url=f"https://github.com/{REPO}/releases",
                   credits="Film data from TMDB (this product uses the TMDB API but is not endorsed or certified by TMDB). Scene data from ThePornDB. Release years can be checked against IMDb Non-Commercial Datasets (datasets.imdbws.com); Film Tagger is not endorsed or certified by IMDb.")

@app.get("/api/updates/check")
def updates_check():
    """?force=1 bypasses the one-hour cache. Only reports; the user opens the release page themselves."""
    return jsonify(check_updates(bool(request.args.get("force"))))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"Film Tagger running: open http://localhost:{port}   (database: {db.DB_PATH})")
    # IMDb title.basics: refresh in the background if the local lookup is missing or older than 7 days (fails softly)
    threading.Thread(target=lambda: (time.sleep(2), imdbmod.kick_refresh(force=False)), daemon=True).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
