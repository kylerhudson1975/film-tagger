"""IMDb Non-Commercial title.basics lookup for release years.

Downloads https://datasets.imdbws.com/title.basics.tsv.gz into the Film Tagger
data folder and builds a separate, replaceable SQLite file (imdb_years.db) with
tconst -> startYear for movie / tvMovie / video / short / tvMiniSeries / tvSpecial
and short-run tvSeries. Never writes films.db.
Refresh is background, non-blocking, and fails softly. Existing films are never
changed by a scan; apply_fixes is the only path that updates years (via the app).
"""
import csv, gzip, io, os, re, sqlite3, threading, time, urllib.error, urllib.request

import db

BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
# Theatrical and made-for-TV "films" (a miniseries or short-run series can be a film in the library).
FILM_TYPES = frozenset(("movie", "tvMovie", "video", "short"))
TV_FILM_TYPES = frozenset(("tvMiniSeries", "tvSpecial", "tvSeries"))
TITLE_TYPES = FILM_TYPES | TV_FILM_TYPES
YEAR_FIX_DELTA = 1  # existing-film year fixes and new-movie auto-fix: only ±1
# Only a single episode is treated as "probably the wrong kind of title" by itself.
# tvMovie / tvMiniSeries / tvSpecial / short tvSeries are valid for a library film.
SUSPECT_TYPES = frozenset(("tvEpisode",))
# Prefer a theatrical movie over a TV movie, then video/short, and only then a
# miniseries/special/short series — and never fall through once a higher group
# had close-year candidates that could not be uniquely picked (Amadeus 1984).
TYPE_GROUPS = (("movie",), ("tvMovie",), ("video",), ("short",), ("tvMiniSeries", "tvSpecial"), ("tvSeries",))
STALE_SEC = 7 * 24 * 3600
UA = "Film Tagger/1.0 (non-commercial; IMDb datasets.imdbws.com title.basics refresh)"
SCHEMA = """
CREATE TABLE titles (
  tconst TEXT PRIMARY KEY,
  start_year INTEGER NOT NULL,
  title_type TEXT NOT NULL,
  title_key TEXT NOT NULL,
  orig_key TEXT
);
CREATE INDEX titles_key ON titles(title_key);
CREATE INDEX titles_orig ON titles(orig_key);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Background refresh state (also surfaced as /api/jobs "imdb").
STATE = {
    "running": False, "kind": "imdbyears", "phase": "", "detail": "",
    "done": 0, "total": 0, "error": None, "cancel": False, "finished_at": None,
}

_LOCK = threading.Lock()
_PUNCT = re.compile(r"[^a-z0-9]+")
_IMDB_ID = re.compile(r"(tt\d{5,})", re.I)


def db_path():
    return os.path.join(db.user_data_dir(), "imdb_years.db")


def gz_path():
    return os.path.join(db.user_data_dir(), "title.basics.tsv.gz")


def default_csv_path():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Desktop", "Film Tagger stuff", "imdb_year_mismatches.csv")


def default_backfill_csv_path():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Desktop", "Film Tagger stuff", "imdb_id_backfill.csv")


def title_key(s):
    """Equality key: fold accents/case, drop punctuation, collapse spaces."""
    s = db.fold(s or "")
    s = s.replace("'", "").replace("\u2019", "")
    return " ".join(_PUNCT.sub(" ", s).split())


def parse_imdb_id(val):
    if val is None: return None
    s = str(val).strip()
    if not s: return None
    m = _IMDB_ID.search(s)
    if m: return m.group(1).lower()
    if s.isdigit() and len(s) >= 5: return "tt" + s.zfill(7)
    return None


def is_short_tv_series(start_year, end_year):
    """True when a tvSeries run is short enough to treat as a made-for-TV film (same year or ±1)."""
    if end_year in (None, "", r"\N"):
        return False
    try:
        return abs(int(end_year) - int(start_year)) <= 1
    except (TypeError, ValueError):
        return False


def include_title_type(typ, start_year, end_year=None):
    typ = (typ or "").strip()
    if typ not in TITLE_TYPES:
        return False
    if typ == "tvSeries":
        return is_short_tv_series(start_year, end_year)
    return True


def imdb_id_from_tmdb(movie):
    """IMDb id from a TMDB movie payload (top-level imdb_id or external_ids). Never searches by title."""
    movie = movie or {}
    ext = movie.get("external_ids") if isinstance(movie.get("external_ids"), dict) else {}
    return parse_imdb_id(movie.get("imdb_id") or (ext or {}).get("imdb_id"))


def _tmdb_year(movie):
    d = str((movie or {}).get("release_date") or (movie or {}).get("first_air_date") or "")
    return int(d[:4]) if len(d) >= 4 and d[:4].isdigit() else None


def titles_agree_names(film_title, film_original, other_title, other_original=None):
    """True when the film title/original matches another work's title or original title (folded keys)."""
    return titles_agree(film_title, film_original, {
        "title_key": title_key(other_title) if other_title else None,
        "orig_key": title_key(other_original) if other_original else None,
    })


def decide_imdb_id(film, movie=None, *, require_close_year=True):
    """Decide whether to store an IMDb id from a TMDB movie record. Never uses a title-only IMDb search.

    film: mapping with title, original_title, year, tmdb_id, imdb_id, edited_fields, kind.
    movie: TMDB /movie/{id} (or /external_ids) payload for that stored tmdb_id, or None if not fetched.
    require_close_year: backfill requires the TMDB year to be within ±1 of the film's year (a solid match).

    Returns dict action ('fill' or 'skip'), imdb_id, reason.
    """
    film = film or {}
    try:
        ed = film.get("edited_fields")
        edited = set(ed if isinstance(ed, (list, set, tuple)) else (__import__("json").loads(ed or "[]") or []))
    except Exception:
        edited = set()
    existing = parse_imdb_id(film.get("imdb_id"))
    if "imdb_id" in edited and existing:
        return {"action": "skip", "imdb_id": existing, "reason": "user_set"}
    if existing:
        return {"action": "skip", "imdb_id": existing, "reason": "already_has_imdb_id"}
    if (film.get("kind") or "") == "scene":
        return {"action": "skip", "imdb_id": "", "reason": "scene"}
    if not film.get("tmdb_id"):
        return {"action": "skip", "imdb_id": "", "reason": "no_tmdb_id"}
    if movie is None:
        return {"action": "skip", "imdb_id": "", "reason": "tmdb_not_fetched"}
    if not titles_agree_names(film.get("title"), film.get("original_title"),
                              movie.get("title") or movie.get("name"),
                              movie.get("original_title") or movie.get("original_name")):
        return {"action": "skip", "imdb_id": "", "reason": "tmdb_title_mismatch"}
    if require_close_year:
        fy, ty = _film_year(film.get("year")), _tmdb_year(movie)
        if fy is not None and ty is not None and abs(fy - ty) > YEAR_FIX_DELTA:
            return {"action": "skip", "imdb_id": "", "reason": "tmdb_year_mismatch"}
    iid = imdb_id_from_tmdb(movie)
    if not iid:
        return {"action": "skip", "imdb_id": "", "reason": "tmdb_has_no_imdb_id"}
    hit = lookup(iid)
    if not hit:
        return {"action": "skip", "imdb_id": "", "reason": "imdb_id_not_in_dataset"}
    if not titles_agree(film.get("title"), film.get("original_title"), hit):
        return {"action": "skip", "imdb_id": "", "reason": "imdb_title_mismatch"}
    return {"action": "fill", "imdb_id": iid, "reason": "tmdb_external_ids"}


BACKFILL_CSV_FIELDS = ["film_id", "title", "year", "library", "tmdb_id", "tmdb_kind", "imdb_id", "action", "reason"]


def write_backfill_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=BACKFILL_CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in BACKFILL_CSV_FIELDS})
    return path


def adjust_release_date(rd, year):
    """Keep month/day only when the year already matches; otherwise store YYYY only."""
    year = int(year)
    s = (rd or "").strip()
    m = re.match(r"^(\d{4})(.*)$", s)
    if m and int(m.group(1)) == year:
        return s
    return f"{year:04d}"


def _connect(path=None, create=False):
    p = path or db_path()
    if not create and not os.path.exists(p):
        return None
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    c = sqlite3.connect(p, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _meta(c, key, default=None):
    if c is None: return default
    r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def _set_meta(c, **kv):
    for k, v in kv.items():
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, "" if v is None else str(v)))


def _float_meta(c, key):
    v = _meta(c, key)
    try: return float(v) if v not in (None, "") else None
    except (TypeError, ValueError): return None


def status():
    """Public dataset status for Settings and the API."""
    out = {
        "ready": False, "rows": 0, "built_at": None, "downloaded_at": None,
        "checked_at": None, "source_date": None, "source_last_modified": None,
        "stale": True, "path": db_path(), "gz_path": gz_path(),
        "refreshing": bool(STATE.get("running")), "phase": STATE.get("phase") or "",
        "detail": STATE.get("detail") or "", "error": STATE.get("error"),
        "url": BASICS_URL,
    }
    c = None
    try:
        c = _connect(create=False)
        if c is None:
            return out
        try:
            out["rows"] = int(_meta(c, "rows") or 0)
        except Exception:
            out["rows"] = 0
        if not out["rows"]:
            try:
                out["rows"] = c.execute("SELECT COUNT(*) FROM titles").fetchone()[0]
            except Exception:
                out["rows"] = 0
        out["built_at"] = _float_meta(c, "built_at")
        out["downloaded_at"] = _float_meta(c, "downloaded_at")
        out["checked_at"] = _float_meta(c, "checked_at")
        out["source_date"] = _meta(c, "source_date") or None
        out["source_last_modified"] = _meta(c, "source_last_modified") or None
        if not out["source_date"] and out["source_last_modified"]:
            out["source_date"] = _source_date_from_http(out["source_last_modified"])
        out["ready"] = out["rows"] > 0
        age_from = out["checked_at"] or out["built_at"] or out["downloaded_at"]
        out["stale"] = (not out["ready"]) or (not age_from) or (time.time() - age_from > STALE_SEC)
        if not out["error"]:
            out["error"] = _meta(c, "last_error") or None
        return out
    except Exception as e:
        out["error"] = str(e)
        return out
    finally:
        if c is not None:
            try: c.close()
            except Exception: pass


def _source_date_from_http(last_modified):
    """'Tue, 10 Nov 2026 12:47:20 GMT' -> '2026-11-10'."""
    if not last_modified: return None
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(last_modified)
        return dt.date().isoformat()
    except Exception:
        return None


def needs_refresh():
    s = status()
    return (not s["ready"]) or s["stale"]


def year_for(tconst):
    iid = parse_imdb_id(tconst)
    if not iid: return None
    c = _connect(create=False)
    if c is None: return None
    try:
        r = c.execute("SELECT start_year FROM titles WHERE tconst=?", (iid,)).fetchone()
        return int(r[0]) if r else None
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def lookup(tconst):
    iid = parse_imdb_id(tconst)
    if not iid: return None
    c = _connect(create=False)
    if c is None: return None
    try:
        r = c.execute(
            "SELECT tconst, start_year, title_type, title_key, orig_key FROM titles WHERE tconst=?",
            (iid,),
        ).fetchone()
        if not r: return None
        return _hit_from_row(r, how="imdb_id")
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _hit_from_row(r, how="imdb_id"):
    keys = set(r.keys()) if hasattr(r, "keys") else set()
    d = {
        "tconst": r["tconst"],
        "year": int(r["start_year"]),
        "title_type": r["title_type"],
        "how": how,
        "title_key": r["title_key"] if "title_key" in keys else None,
        "orig_key": r["orig_key"] if "orig_key" in keys else None,
    }
    for k in ("primary_title", "original_title", "genres"):
        if k in keys:
            d[k] = r[k]
    return d


def titles_agree(film_title, original, hit):
    """True when the library title matches IMDb primaryTitle or originalTitle (folded keys)."""
    if not hit:
        return False
    keys = []
    for s in (film_title, original):
        k = title_key(s)
        if k and k not in keys:
            keys.append(k)
    if not keys:
        return False
    return (hit.get("title_key") in keys) or (hit.get("orig_key") in keys)


def type_mismatch(hit, is_classic=False):
    """True when the IMDb title is probably not this library film (a single TV episode,
    or a documentary vs Classic Porn). tvMovie / tvMiniSeries / tvSpecial / short
    tvSeries are allowed — those are how IMDb files many made-for-TV films."""
    typ = (hit or {}).get("title_type") or ""
    genres = ((hit or {}).get("genres") or "").lower()
    if typ in SUSPECT_TYPES:
        return True
    if is_classic and typ not in ("movie", "video", "tvMovie"):
        return True
    if is_classic and "documentary" in genres:
        return True
    return False


def _pick_match(rows, year=None):
    if not rows: return None
    by_type = {}
    for r in rows:
        by_type.setdefault(r["title_type"], []).append(r)
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    delayed = None  # unique higher-type hit whose year is far off; used only if no closer lower-type match
    for group in TYPE_GROUPS:
        cand = [r for t in group for r in by_type.get(t, [])]
        if not cand:
            continue
        ids = {r["tconst"] for r in cand}
        if len(ids) == 1:
            r = cand[0]
            if year is None or abs(int(r["start_year"]) - year) <= YEAR_FIX_DELTA:
                return _hit_from_row(r, how="unique_title")
            if delayed is None:
                delayed = _hit_from_row(r, how="unique_title")
            continue
        if year is None:
            return None  # ambiguous at this preferred type; do not fall through
        exact = [r for r in cand if int(r["start_year"]) == year]
        eids = {r["tconst"] for r in exact}
        if len(eids) == 1:
            return _hit_from_row(exact[0], how="exact_year")
        if len(eids) > 1:
            return None
        near = [r for r in cand if abs(int(r["start_year"]) - year) == 1]
        nids = {r["tconst"] for r in near}
        if len(nids) == 1:
            return _hit_from_row(near[0], how="year_plus_minus_1")
        if len(nids) > 1:
            return None
        # This group only has far-off years: try miniseries/series before giving up
        continue
    return delayed


def find_by_title(title, original=None, year=None):
    """Resolve a title to a unique IMDb title (prefer movie/tvMovie). None if ambiguous."""
    keys = []
    for s in (title, original):
        k = title_key(s)
        if k and k not in keys: keys.append(k)
    if not keys: return None
    c = _connect(create=False)
    if c is None: return None
    try:
        ph = ",".join("?" * len(keys))
        rows = c.execute(
            f"SELECT tconst, start_year, title_type, title_key, orig_key FROM titles "
            f"WHERE title_key IN ({ph}) OR orig_key IN ({ph})",
            keys + keys,
        ).fetchall()
        return _pick_match(rows, year)
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def resolve_film(title, original=None, year=None, imdb_id=None):
    """Prefer a stored/known IMDb id; otherwise a unique title match."""
    hit = lookup(imdb_id) if imdb_id else None
    if hit: return hit
    return find_by_title(title, original, year)


def enrich_tmdb_apply(upd, lock, movie, film_title=None, film_original=None):
    """Mutate a TMDB apply_movie update with imdb_id and IMDb year. Soft-fail if the dataset is missing.
    Stores the IMDb id only when the IMDb title matches this film (or the TMDB title if the film
    title is not available). Never looks the id up by title. Does not override year/release_date
    when those fields are locked (hand-edited or keep=), and never overwrites a user-set imdb_id."""
    lock = set(lock or ())
    iid = imdb_id_from_tmdb(movie)
    if not iid:
        return False
    hit = lookup(iid)
    # Prefer the library title; fall back to the TMDB title when this is a brand-new apply
    agree = False
    if hit:
        if film_title or film_original:
            agree = titles_agree(film_title, film_original, hit)
        else:
            agree = titles_agree((movie or {}).get("title"), (movie or {}).get("original_title"), hit)
    if agree and "imdb_id" not in lock:
        upd["imdb_id"] = iid
    if not agree:
        return False
    if lock & {"year", "release_date"}:
        return False
    y = int(hit["year"])
    try:
        cur = int(upd["year"]) if upd.get("year") not in (None, "") else None
    except (TypeError, ValueError):
        cur = None
    if cur == y:
        return False
    if cur is None or abs(cur - y) > YEAR_FIX_DELTA:
        return False
    if type_mismatch(hit, is_classic=False):
        return False
    upd["year"] = y
    upd["release_date"] = adjust_release_date(upd.get("release_date"), y)
    return True


def library_name(kind, path, is_classic=False):
    if (kind or "") == "scene":
        return "Porn Scenes"
    if is_classic:
        return "Classic Porn"
    p = (path or "").replace("/", "\\").rstrip("\\")
    low = p.lower()
    if "classic porn" in low:
        return "Classic Porn"
    # last remaining folder after the drive: D:\Movies\...
    parts = [x for x in p.split("\\") if x]
    if len(parts) >= 2 and re.match(r"^[A-Za-z]:$", parts[0]):
        return parts[1]
    if parts:
        return parts[0]
    return "Movies"


def _film_year(val):
    if val in (None, ""): return None
    try: return int(val)
    except (TypeError, ValueError):
        s = str(val).strip()
        return int(s[:4]) if len(s) >= 4 and s[:4].isdigit() else None


def _hand_edited_year(edited_fields):
    try:
        ed = set(edited_fields if isinstance(edited_fields, (list, set, tuple)) else (__import__("json").loads(edited_fields or "[]") or []))
    except Exception:
        ed = set()
    return bool(ed & {"year", "release_date"})


def _entry(r, hit, is_classic, section, reason=""):
    cur = _film_year(r["year"])
    iy = int(hit["year"]) if hit and hit.get("year") is not None else None
    return {
        "film_id": r["id"],
        "title": r["title"] or "",
        "year": cur,
        "imdb_year": iy,
        "imdb_id": (hit or {}).get("tconst") or parse_imdb_id(r["imdb_id"]) or "",
        "imdb_title": (hit or {}).get("primary_title") or "",
        "library": library_name(r["kind"] or "", r["path"], is_classic),
        "hand_edited": _hand_edited_year(r["edited_fields"]),
        "how": (hit or {}).get("how") or "",
        "title_type": (hit or {}).get("title_type") or "",
        "section": section,
        "reason": reason,
    }


def classify_films(c):
    """Split the library into apply-able year fixes vs suspected wrong IMDb links.
    Year fixes require the film's own stored imdb_id, a matching IMDb title, and a year
    off by at most YEAR_FIX_DELTA (±1). Title-only fallback is never a year fix.
    Same-title / large-gap / type-mismatch hits go in wrong_links (info only).
    Read-only on films.db. Porn Scenes are only considered when they already have an imdb_id."""
    year_fixes, wrong_links = [], []
    if not status().get("ready"):
        return {"year_fixes": [], "wrong_links": []}
    rows = c.execute(
        "SELECT f.id, f.title, f.original_title, f.year, f.imdb_id, f.kind, f.edited_fields, "
        "(SELECT path FROM files WHERE film_id=f.id ORDER BY id LIMIT 1) AS path "
        "FROM films f"
    ).fetchall()
    classic_ids = {r[0] for r in c.execute(
        "SELECT DISTINCT film_id FROM files WHERE " + db.CLASSIC_PORN_SQL
    )}
    for r in rows:
        fid = r["id"]
        kind = r["kind"] or ""
        iid = parse_imdb_id(r["imdb_id"])
        is_classic = fid in classic_ids or "classic porn" in ((r["path"] or "").lower())
        cur = _film_year(r["year"])
        if iid:
            hit = lookup(iid)
            if not hit:
                wrong_links.append(_entry(r, {"tconst": iid, "year": None, "title_type": "", "how": "imdb_id"},
                                          is_classic, "wrong_link", "imdb_id_not_in_dataset"))
                continue
            agree = titles_agree(r["title"], r["original_title"], hit)
            typ_bad = type_mismatch(hit, is_classic)
            gap = None if cur is None or hit.get("year") is None else abs(cur - int(hit["year"]))
            if agree and not typ_bad and gap == 0:
                continue
            if agree and not typ_bad and gap is not None and 1 <= gap <= YEAR_FIX_DELTA:
                year_fixes.append(_entry(r, hit, is_classic, "year_fix", "stored_imdb_id"))
                continue
            reasons = []
            if not agree:
                reasons.append("title_mismatch")
            if typ_bad:
                reasons.append("type_mismatch")
            if gap is None or gap > YEAR_FIX_DELTA:
                reasons.append("year_gap")
            wrong_links.append(_entry(r, hit, is_classic, "wrong_link", ",".join(reasons) or "mismatch"))
            continue
        # No stored IMDb id: never a year fix. Same-title hits are listed as possible different films.
        if kind == "scene":
            continue
        hit = find_by_title(r["title"], r["original_title"], r["year"])
        if not hit:
            continue
        iy = int(hit["year"])
        if cur == iy:
            continue
        reasons = ["no_stored_imdb_id"]
        if not titles_agree(r["title"], r["original_title"], hit):
            reasons.append("title_mismatch")
        if type_mismatch(hit, is_classic):
            reasons.append("type_mismatch")
        if cur is None or abs(cur - iy) > YEAR_FIX_DELTA:
            reasons.append("year_gap")
        else:
            reasons.append("close_year_but_no_imdb_id")
        wrong_links.append(_entry(r, hit, is_classic, "wrong_link", ",".join(reasons)))
    key = lambda x: ((x["title"] or "").casefold(), x["film_id"])
    year_fixes.sort(key=key)
    wrong_links.sort(key=key)
    return {"year_fixes": year_fixes, "wrong_links": wrong_links}


def find_mismatches(c):
    """Apply-able year fixes only (stored IMDb id, matching title, year ±1)."""
    return classify_films(c)["year_fixes"]


def counts_by_library(rows):
    d = {}
    for r in rows:
        d[r["library"]] = d.get(r["library"], 0) + 1
    return d


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["section", "title", "current_year", "imdb_year", "imdb_id", "imdb_title", "title_type", "library", "film_id", "hand_edited", "how", "reason"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "section": r.get("section") or "",
                "title": r.get("title") or "",
                "current_year": r.get("year") if r.get("year") is not None else "",
                "imdb_year": r.get("imdb_year") if r.get("imdb_year") is not None else "",
                "imdb_id": r.get("imdb_id") or "",
                "imdb_title": r.get("imdb_title") or "",
                "title_type": r.get("title_type") or "",
                "library": r.get("library") or "",
                "film_id": r.get("film_id") or "",
                "hand_edited": "yes" if r.get("hand_edited") else "",
                "how": r.get("how") or "",
                "reason": r.get("reason") or "",
            })
    return path


def apply_fixes(c, film_ids, now=None):
    """Update selected films' year/release_date/imdb_id. Caller commits and
    invalidates Metacritic. Returns {applied, skipped, changed:[{film_id, year, imdb_year}]}."""
    now = time.time() if now is None else now
    want = [int(x) for x in (film_ids or [])]
    applied, skipped, changed = 0, 0, []
    if not want:
        return {"applied": 0, "skipped": 0, "changed": []}
    ph = ",".join("?" * len(want))
    rows = {r["id"]: r for r in c.execute(
        f"SELECT id, title, original_title, year, release_date, imdb_id, kind, edited_fields FROM films WHERE id IN ({ph})",
        want,
    )}
    for fid in want:
        r = rows.get(fid)
        if not r:
            skipped += 1
            continue
        kind = r["kind"] or ""
        iid = parse_imdb_id(r["imdb_id"])
        if not iid:
            skipped += 1
            continue
        hit = lookup(iid)
        if not hit or not titles_agree(r["title"], r["original_title"], hit):
            skipped += 1
            continue
        path = (c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id LIMIT 1", (fid,)).fetchone() or [None])[0]
        is_classic = "classic porn" in ((path or "").lower()) or (kind != "scene" and db.is_classic_porn(c, fid))
        if type_mismatch(hit, is_classic):
            skipped += 1
            continue
        iy = int(hit["year"])
        cur = _film_year(r["year"])
        if cur is None or abs(cur - iy) > YEAR_FIX_DELTA:
            skipped += 1
            continue
        new_rd = adjust_release_date(r["release_date"], iy)
        sets = {"imdb_id": hit["tconst"], "updated_at": now}
        year_changed = cur != iy
        if year_changed:
            sets["year"] = iy
            sets["release_date"] = new_rd
        c.execute(
            f"UPDATE films SET {','.join(k+'=?' for k in sets)} WHERE id=?",
            list(sets.values()) + [fid],
        )
        applied += 1
        if year_changed:
            changed.append({"film_id": fid, "year": cur, "imdb_year": iy, "imdb_id": hit["tconst"]})
    return {"applied": applied, "skipped": skipped, "changed": changed}


def _set_state(**kw):
    STATE.update(kw)


def _download(dest, last_modified=None):
    """Stream title.basics.tsv.gz to dest. Returns 'not_modified' or a headers dict."""
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(BASICS_URL, headers=headers)
    partial = dest + ".partial"
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "not_modified"
        raise
    try:
        total = int(resp.headers.get("Content-Length") or 0)
        _set_state(phase="download", done=0, total=total or 0, detail="Downloading title.basics.tsv.gz")
        got = 0
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(partial, "wb") as f:
            while True:
                if STATE.get("cancel"):
                    raise RuntimeError("cancelled")
                chunk = resp.read(1024 * 256)
                if not chunk: break
                f.write(chunk)
                got += len(chunk)
                _set_state(done=got, total=total or got, detail=f"Downloaded {got // (1024*1024)} MB")
        os.replace(partial, dest)
        lm = resp.headers.get("Last-Modified")
        run = resp.headers.get("x-amz-meta-run-date") or _source_date_from_http(lm)
        return {"last_modified": lm, "source_date": run, "bytes": got}
    finally:
        try: resp.close()
        except Exception: pass
        if os.path.exists(partial):
            try: os.remove(partial)
            except Exception: pass


def _iter_basics(path):
    """Yield (tconst, title_type, start_year, title_key, orig_key) from a .gz or .tsv file."""
    if path.endswith(".gz"):
        fh = gzip.open(path, "rt", encoding="utf-8", newline="")
    else:
        fh = open(path, "r", encoding="utf-8", newline="")
    try:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            typ = (row.get("titleType") or "").strip()
            year = (row.get("startYear") or "").strip()
            if not year or year == "\\N":
                continue
            try:
                year = int(year)
            except ValueError:
                continue
            if not include_title_type(typ, year, (row.get("endYear") or "").strip()):
                continue
            tconst = (row.get("tconst") or "").strip()
            if not tconst:
                continue
            pk = title_key(row.get("primaryTitle"))
            ok = title_key(row.get("originalTitle")) or None
            if not pk:
                continue
            yield tconst, typ, year, pk, (ok if ok != pk else ok)
    finally:
        fh.close()


def build_from_file(src, dest=None, extra_meta=None, progress=True):
    """Build imdb_years.db from a title.basics .gz or .tsv. Replaces dest atomically."""
    dest = dest or db_path()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".building"
    if os.path.exists(tmp):
        try: os.remove(tmp)
        except Exception: pass
    c = sqlite3.connect(tmp)
    try:
        c.execute("PRAGMA journal_mode=OFF")
        c.execute("PRAGMA synchronous=OFF")
        c.executescript(SCHEMA)
        n = 0
        batch = []
        _set_state(phase="build", done=0, total=0, detail="Building IMDb year lookup")
        for rec in _iter_basics(src):
            if STATE.get("cancel"):
                raise RuntimeError("cancelled")
            batch.append(rec)
            if len(batch) >= 5000:
                c.executemany("INSERT OR REPLACE INTO titles(tconst,title_type,start_year,title_key,orig_key) VALUES(?,?,?,?,?)", batch)
                n += len(batch)
                batch = []
                if progress:
                    _set_state(done=n, detail=f"Indexed {n:,} titles")
        if batch:
            c.executemany("INSERT OR REPLACE INTO titles(tconst,title_type,start_year,title_key,orig_key) VALUES(?,?,?,?,?)", batch)
            n += len(batch)
        now = str(time.time())
        meta = {
            "rows": str(n),
            "built_at": now,
            "checked_at": now,
            "source_file": os.path.abspath(src),
        }
        if extra_meta:
            meta.update({k: ("" if v is None else str(v)) for k, v in extra_meta.items()})
        for k, v in meta.items():
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))
        c.commit()
    except Exception:
        try: c.close()
        except Exception: pass
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass
        raise
    c.close()
    os.replace(tmp, dest)
    _set_state(done=n, detail=f"Indexed {n:,} titles")
    return n


def build_from_rows(rows, dest=None, extra_meta=None):
    """Test helper: rows are dicts or (tconst, titleType, primaryTitle, originalTitle, startYear)."""
    dest = dest or db_path()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".building"
    if os.path.exists(tmp):
        try: os.remove(tmp)
        except Exception: pass
    c = sqlite3.connect(tmp)
    try:
        c.executescript(SCHEMA)
        recs = []
        for row in rows:
            if not isinstance(row, dict):
                tconst, typ, primary, original, year, *rest = row
                row = {"tconst": tconst, "titleType": typ, "primaryTitle": primary, "originalTitle": original or primary, "startYear": year, "endYear": (rest[0] if rest else None)}
            typ = (row.get("titleType") or "").strip()
            year = row.get("startYear")
            if year in (None, "", "\\N"):
                continue
            year = int(year)
            if not include_title_type(typ, year, row.get("endYear")):
                continue
            tconst = (row.get("tconst") or "").strip()
            pk = title_key(row.get("primaryTitle"))
            ok = title_key(row.get("originalTitle")) or None
            if not tconst or not pk:
                continue
            recs.append((tconst, typ, year, pk, ok))
        c.executemany("INSERT OR REPLACE INTO titles(tconst,title_type,start_year,title_key,orig_key) VALUES(?,?,?,?,?)", recs)
        now = str(time.time())
        meta = {"rows": str(len(recs)), "built_at": now, "checked_at": now, "source_date": "test"}
        if extra_meta:
            meta.update({k: str(v) for k, v in extra_meta.items()})
        for k, v in meta.items():
            c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (k, v))
        c.commit()
    except Exception:
        try: c.close()
        except Exception: pass
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass
        raise
    c.close()
    os.replace(tmp, dest)
    return len(recs)


def refresh(force=False, reuse_gz=False):
    """Download (if needed) and rebuild the lookup. Fail softly; keep the old db on error.
    reuse_gz=True rebuilds from the copy already on disk (used when the import types change)."""
    with _LOCK:
        if STATE.get("running"):
            return status()
        _set_state(running=True, kind="imdbyears", phase="start", detail="", done=0, total=0, error=None, cancel=False, finished_at=None)
    try:
        st = status()
        if not force and not reuse_gz and st.get("ready") and not st.get("stale"):
            _set_state(phase="idle", detail="Already up to date")
            return status()
        os.makedirs(db.user_data_dir(), exist_ok=True)
        dest_gz = gz_path()
        dest_db = db_path()
        info = None
        if reuse_gz and os.path.exists(dest_gz):
            _set_state(detail="Rebuilding from the title.basics copy on disk")
            info = {
                "last_modified": st.get("source_last_modified"),
                "source_date": st.get("source_date"),
            }
        else:
            try:
                info = _download(dest_gz, last_modified=st.get("source_last_modified") if st.get("ready") and not force else None)
            except Exception as e:
                if os.path.exists(dest_gz) and (force or not st.get("ready") or st.get("stale") or reuse_gz):
                    # Fall back to a previously downloaded gz
                    _set_state(detail=f"Download failed ({e}); rebuilding from the copy on disk")
                    info = {
                        "last_modified": st.get("source_last_modified"),
                        "source_date": st.get("source_date"),
                        "download_error": str(e),
                    }
                else:
                    raise
        if info == "not_modified":
            c = _connect(create=False)
            if c is not None:
                try:
                    _set_meta(c, checked_at=str(time.time()), last_error="")
                    c.commit()
                finally:
                    c.close()
            _set_state(phase="idle", detail="Dataset unchanged (not modified)")
            return status()
        extra = {
            "downloaded_at": str(time.time()),
            "checked_at": str(time.time()),
            "source_last_modified": (info or {}).get("last_modified") or "",
            "source_date": (info or {}).get("source_date") or "",
            "last_error": "",
        }
        if (info or {}).get("download_error"):
            extra["last_error"] = "Download failed; used the copy already on disk: " + info["download_error"]
        build_from_file(dest_gz, dest=dest_db, extra_meta=extra)
        _set_state(phase="idle", detail="Ready")
        return status()
    except Exception as e:
        _set_state(error=str(e), phase="error", detail=str(e))
        try:
            c = _connect(create=False)
            if c is not None:
                _set_meta(c, last_error=str(e), checked_at=str(time.time()))
                c.commit()
                c.close()
        except Exception:
            pass
        return status()
    finally:
        _set_state(running=False, finished_at=time.time())


def kick_refresh(force=False, reuse_gz=False):
    """Start a background refresh if needed (or if force). Returns True if a thread was started."""
    if STATE.get("running"):
        return False
    if not force and not reuse_gz and not needs_refresh():
        return False
    def run():
        try:
            refresh(force=force, reuse_gz=reuse_gz)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="imdb-years-refresh").start()
    return True
