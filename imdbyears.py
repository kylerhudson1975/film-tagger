"""IMDb Non-Commercial title.basics lookup for release years.

Downloads https://datasets.imdbws.com/title.basics.tsv.gz into the Film Tagger
data folder and builds a separate, replaceable SQLite file (imdb_years.db) with
tconst -> startYear for movie / tvMovie / video / short. Never writes films.db.
Refresh is background, non-blocking, and fails softly. Existing films are never
changed by a scan; apply_fixes is the only path that updates years (via the app).
"""
import csv, gzip, io, os, re, sqlite3, threading, time, urllib.error, urllib.request

import db

BASICS_URL = "https://datasets.imdbws.com/title.basics.tsv.gz"
TITLE_TYPES = frozenset(("movie", "tvMovie", "video", "short"))
# Prefer a theatrical movie over a TV movie, and never fall through to video/short
# once a higher type group had any candidates (avoids "Amadeus" 1984 matching a 2018 video).
TYPE_GROUPS = (("movie",), ("tvMovie",), ("video",), ("short",))
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
        r = c.execute("SELECT tconst, start_year, title_type FROM titles WHERE tconst=?", (iid,)).fetchone()
        if not r: return None
        return {"tconst": r["tconst"], "year": int(r["start_year"]), "title_type": r["title_type"], "how": "imdb_id"}
    except Exception:
        return None
    finally:
        try: c.close()
        except Exception: pass


def _pick_match(rows, year=None):
    if not rows: return None
    by_type = {}
    for r in rows:
        by_type.setdefault(r["title_type"], []).append(r)
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    for group in TYPE_GROUPS:
        cand = [r for t in group for r in by_type.get(t, [])]
        if not cand:
            continue
        ids = {r["tconst"] for r in cand}
        if len(ids) == 1:
            r = cand[0]
            return {"tconst": r["tconst"], "year": int(r["start_year"]), "title_type": r["title_type"], "how": "unique_title"}
        if year is None:
            return None  # ambiguous at this preferred type; do not fall through to video/short
        exact = [r for r in cand if int(r["start_year"]) == year]
        eids = {r["tconst"] for r in exact}
        if len(eids) == 1:
            r = exact[0]
            return {"tconst": r["tconst"], "year": int(r["start_year"]), "title_type": r["title_type"], "how": "exact_year"}
        if len(eids) > 1:
            return None
        near = [r for r in cand if abs(int(r["start_year"]) - year) == 1]
        nids = {r["tconst"] for r in near}
        if len(nids) == 1:
            r = near[0]
            return {"tconst": r["tconst"], "year": int(r["start_year"]), "title_type": r["title_type"], "how": "year_plus_minus_1"}
        return None
    return None


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


def enrich_tmdb_apply(upd, lock, movie):
    """Mutate a TMDB apply_movie update with imdb_id and IMDb year. Soft-fail if the dataset is missing.
    Does not override year/release_date when those fields are locked (hand-edited or keep=)."""
    lock = set(lock or ())
    iid = parse_imdb_id((movie or {}).get("imdb_id"))
    if not iid:
        return False
    if "imdb_id" not in lock:
        upd["imdb_id"] = iid
    if lock & {"year", "release_date"}:
        return False
    y = year_for(iid)
    if not y:
        return False
    try:
        cur = int(upd["year"]) if upd.get("year") not in (None, "") else None
    except (TypeError, ValueError):
        cur = None
    if cur == y:
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


def find_mismatches(c):
    """Compare existing films to IMDb years. Read-only on films.db (never writes).
    Porn Scenes are included only when they already have an imdb_id. Movies and
    Classic Porn also try a unique title match when no id is stored."""
    import json as _json
    if not status().get("ready"):
        return []
    rows = c.execute(
        "SELECT f.id, f.title, f.original_title, f.year, f.imdb_id, f.kind, f.edited_fields, "
        "(SELECT path FROM files WHERE film_id=f.id ORDER BY id LIMIT 1) AS path "
        "FROM films f"
    ).fetchall()
    classic_ids = {r[0] for r in c.execute(
        "SELECT DISTINCT film_id FROM files WHERE " + db.CLASSIC_PORN_SQL
    )}
    out = []
    for r in rows:
        fid = r["id"]
        kind = r["kind"] or ""
        iid = parse_imdb_id(r["imdb_id"])
        if kind == "scene" and not iid:
            continue
        hit = resolve_film(r["title"], r["original_title"], r["year"], iid)
        if not hit:
            continue
        cur = _film_year(r["year"])
        iy = int(hit["year"])
        if cur == iy:
            continue
        is_classic = fid in classic_ids or "classic porn" in ((r["path"] or "").lower())
        out.append({
            "film_id": fid,
            "title": r["title"] or "",
            "year": cur,
            "imdb_year": iy,
            "imdb_id": hit["tconst"],
            "library": library_name(kind, r["path"], is_classic),
            "hand_edited": _hand_edited_year(r["edited_fields"]),
            "how": hit.get("how") or "",
            "title_type": hit.get("title_type") or "",
        })
    out.sort(key=lambda x: ((x["title"] or "").casefold(), x["film_id"]))
    return out


def counts_by_library(rows):
    d = {}
    for r in rows:
        d[r["library"]] = d.get(r["library"], 0) + 1
    return d


def write_csv(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["title", "current_year", "imdb_year", "imdb_id", "library", "film_id", "hand_edited", "how"])
        w.writeheader()
        for r in rows:
            w.writerow({
                "title": r.get("title") or "",
                "current_year": r.get("year") if r.get("year") is not None else "",
                "imdb_year": r.get("imdb_year") if r.get("imdb_year") is not None else "",
                "imdb_id": r.get("imdb_id") or "",
                "library": r.get("library") or "",
                "film_id": r.get("film_id") or "",
                "hand_edited": "yes" if r.get("hand_edited") else "",
                "how": r.get("how") or "",
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
        if kind == "scene" and not iid:
            skipped += 1
            continue
        hit = resolve_film(r["title"], r["original_title"], r["year"], iid)
        if not hit:
            skipped += 1
            continue
        iy = int(hit["year"])
        cur = _film_year(r["year"])
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
            if typ not in TITLE_TYPES:
                continue
            year = (row.get("startYear") or "").strip()
            if not year or year == "\\N":
                continue
            try:
                year = int(year)
            except ValueError:
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
                tconst, typ, primary, original, year = row
                row = {"tconst": tconst, "titleType": typ, "primaryTitle": primary, "originalTitle": original or primary, "startYear": year}
            typ = (row.get("titleType") or "").strip()
            if typ not in TITLE_TYPES:
                continue
            year = row.get("startYear")
            if year in (None, "", "\\N"):
                continue
            year = int(year)
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


def refresh(force=False):
    """Download (if needed) and rebuild the lookup. Fail softly; keep the old db on error."""
    with _LOCK:
        if STATE.get("running"):
            return status()
        _set_state(running=True, kind="imdbyears", phase="start", detail="", done=0, total=0, error=None, cancel=False, finished_at=None)
    try:
        st = status()
        if not force and st.get("ready") and not st.get("stale"):
            _set_state(phase="idle", detail="Already up to date")
            return status()
        os.makedirs(db.user_data_dir(), exist_ok=True)
        dest_gz = gz_path()
        dest_db = db_path()
        info = None
        try:
            info = _download(dest_gz, last_modified=st.get("source_last_modified") if st.get("ready") and not force else None)
        except Exception as e:
            if os.path.exists(dest_gz) and (force or not st.get("ready") or st.get("stale")):
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


def kick_refresh(force=False):
    """Start a background refresh if needed (or if force). Returns True if a thread was started."""
    if STATE.get("running"):
        return False
    if not force and not needs_refresh():
        return False
    def run():
        try:
            refresh(force=force)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True, name="imdb-years-refresh").start()
    return True
