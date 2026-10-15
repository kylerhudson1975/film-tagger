"""Shared database layer for Film Tagger (used by app.py and smarttag.py)."""
import os, re, sqlite3, json, time, unicodedata

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("FILMTAGGER_DB", os.path.join(BASE, "films.db"))
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v", ".mpg", ".mpeg", ".ts", ".m2ts",
             ".webm", ".flv", ".vob", ".divx", ".ogv", ".3gp", ".rmvb", ".rm", ".asf", ".mts", ".iso"}
DEFAULT_CATEGORIES = ["Genre", "Content", "Themes", "Mood", "Setting", "Era", "Style", "People", "Other", "Kink", "Fetish"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS films(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, year INTEGER, director TEXT, cast_ TEXT,
  runtime INTEGER, plot TEXT, poster TEXT, rating INTEGER, watched INTEGER DEFAULT 0,
  notes TEXT, storage TEXT, tmdb_id INTEGER, missing INTEGER DEFAULT 0,
  added_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, film_id INTEGER REFERENCES films(id) ON DELETE CASCADE,
  path TEXT UNIQUE COLLATE NOCASE, size INTEGER, missing INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_files_film ON files(film_id);
CREATE TABLE IF NOT EXISTS categories(name TEXT PRIMARY KEY COLLATE NOCASE, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tags(id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, category TEXT);
CREATE TABLE IF NOT EXISTS film_tags(
  film_id INTEGER REFERENCES films(id) ON DELETE CASCADE,
  tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE,
  source TEXT DEFAULT 'user', status TEXT DEFAULT 'approved', added_at REAL,
  PRIMARY KEY(film_id, tag_id));
CREATE INDEX IF NOT EXISTS ix_ft_tag ON film_tags(tag_id, status);
CREATE TABLE IF NOT EXISTS smart_queue(film_id INTEGER PRIMARY KEY REFERENCES films(id) ON DELETE CASCADE, added_at REAL);
CREATE TABLE IF NOT EXISTS saved_searches(id INTEGER PRIMARY KEY, name TEXT, query TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_films_title ON films(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_films_year ON films(year);
"""

PEOPLE_COLS = ["writers", "producers", "cinematographer", "composer"]
# role key -> films column holding a comma-separated list of names
PEOPLE_ROLES = {"director": "director", "cast": "cast_", "writer": "writers", "producer": "producers",
                "cinematographer": "cinematographer", "composer": "composer"}

def norm_person(s):
    return " ".join((s or "").split()).casefold()

import unicodedata
def fold(s):
    """Accent- and case-insensitive form: 'Les Démons' -> 'les demons'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()

def title_match(title, original, alts, word):
    """1 if `word` (accent/case-insensitive) occurs in the title, original title or alternative titles."""
    w = fold(word)
    if not w: return 0
    return 1 if any(w in fold(x) for x in (title, original, alts) if x) else 0

def fold_in(field, word):
    """1 if `word` occurs (accent/case-insensitive) anywhere in `field`."""
    w = fold(word)
    return 1 if w and field and w in fold(field) else 0

def person_in(field, name):
    """1 if `name` exactly equals (case/whitespace-insensitive) one comma-separated entry of `field`."""
    if not field or not name: return 0
    n = norm_person(name)
    return 1 if any(norm_person(x) == n for x in field.split(",")) else 0

def fold_key(s):
    """Sort key: strip diacritics (NFKD) and casefold, so 'Élan' sorts with E, not after Z."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()

def fold_collate(a, b):
    ka, kb = fold_key(a), fold_key(b)
    return (ka > kb) - (ka < kb) or (a > b) - (a < b)

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.create_collation("FOLD", fold_collate)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(films)")}
    if "edited_fields" not in cols: c.execute("ALTER TABLE films ADD COLUMN edited_fields TEXT")
    if "tmdb_status" not in cols: c.execute("ALTER TABLE films ADD COLUMN tmdb_status TEXT")
    if "mpaa" not in cols: c.execute("ALTER TABLE films ADD COLUMN mpaa TEXT")
    if "mpaa_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN mpaa_checked INTEGER")
    for col in ("original_title", "alt_titles", "user_akas"):  # nullable; existing rows untouched
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} TEXT")
    c.create_function("title_match", 4, title_match, deterministic=True)
    if "custom_poster" not in cols: c.execute("ALTER TABLE films ADD COLUMN custom_poster TEXT")  # user art; TMDB never touches it
    for col in PEOPLE_COLS:  # extra crew roles (nullable; existing rows untouched)
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} TEXT")
    c.create_function("person_in", 2, person_in, deterministic=True)
    c.create_function("fold_in", 2, fold_in, deterministic=True)
    if "country" not in cols: c.execute("ALTER TABLE films ADD COLUMN country TEXT")  # comma-separated country names
    if "country_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN country_checked INTEGER")  # backfill tried
    if "studio" not in cols: c.execute("ALTER TABLE films ADD COLUMN studio TEXT")  # comma-separated production company names
    if "studio_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN studio_checked INTEGER")  # backfill tried
    for col, typ in (("resolution", "TEXT"), ("res_width", "INTEGER"), ("res_height", "INTEGER"), ("res_checked", "INTEGER")):
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} {typ}")  # nullable; existing rows untouched
    if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
        c.executemany("INSERT OR IGNORE INTO categories(name,sort) VALUES(?,?)",
                      [(n, i) for i, n in enumerate(DEFAULT_CATEGORIES)])
        c.commit()
    return c

def get_setting(c, k, default=None):
    r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default

def set_setting(c, k, v):
    c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, json.dumps(v)))

# ---------- filename guessing ----------
JUNK = re.compile(r"\b(2160p|1080p|720p|480p|4k|uhd|hdr|bluray|blu-ray|brrip|bdrip|dvdrip|dvd|webrip|web-dl|web|hdtv|x264|x265|h264|h265|hevc|xvid|divx|aac|ac3|dts|remux|proper|repack|extended|unrated|directors cut|criterion|yify|rarbg)\b.*$", re.I)
GENERIC = re.compile(r"^(movie|film|video|main|title\d*|vts_\d+_\d+|disc\d*|cd\d+|feature|sample)$", re.I)

def guess_title_year(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    d = os.path.dirname(path)
    while os.path.basename(d).upper() in ("VIDEO_TS", "BDMV", "STREAM", "SUBS", "EXTRAS", "FEATURETTES") and os.path.dirname(d) != d:
        d = os.path.dirname(d)
    parent = os.path.basename(d)
    cands = [stem, parent] if not (GENERIC.match(stem.strip()) or os.path.basename(os.path.dirname(path)).upper() == "VIDEO_TS") else [parent, stem]
    for name in cands:
        s = re.sub(r"[._]+", " ", name)
        s = re.sub(r"\[((?:19|20)\d\d)\]", r" \1 ", s); s = re.sub(r"\[[^\]]*\]", " ", s)
        year = None
        m = None
        for m_ in re.finditer(r"(?:^|[\s(\-])((?:19|20)\d\d)(?=$|[\s)\-])", s):
            m = m_
        if m and m.start(1) > 0:
            year = int(m.group(1)); s = s[:m.start(1)]
        s = JUNK.sub("", s)
        s = re.sub(r"[\s(\-]+$", "", re.sub(r"\s+", " ", s)).strip(" -()")
        if s and not GENERIC.match(s):
            if year is None and name is cands[0] and len(cands) > 1:
                # try year from the parent folder if it names the same film
                p = re.sub(r"[._]+", " ", cands[1]); pm = re.search(r"\b((?:19|20)\d\d)\b", p)
                if pm and s.lower() in p.lower(): year = int(pm.group(1))
            return s, year
    return stem, None

# ---------- scanning (read-only) ----------
def scan(c, roots, progress=None):
    progress = progress if progress is not None else {}
    now = time.time(); added = 0; seen = set(); errors = []
    for root in roots:
        if not os.path.isdir(root):
            errors.append(f"Not a folder or not reachable: {root}"); continue
        for dp, dns, fns in os.walk(root, onerror=lambda e: errors.append(str(e))):
            dns[:] = [d for d in dns if not d.startswith(("$", "."))]
            if progress.get("cancel"): break
            progress.update(found=len(seen), added=added, current=dp)
            for fn in fns:
                if os.path.splitext(fn)[1].lower() not in VIDEO_EXT: continue
                if re.search(r"\bsample\b", fn, re.I): continue
                p = os.path.join(dp, fn); seen.add(os.path.normcase(p))
                r = c.execute("SELECT id, film_id FROM files WHERE path=?", (p,)).fetchone()
                try: size = os.path.getsize(p)
                except OSError: size = None
                if r:
                    c.execute("UPDATE files SET missing=0, size=? WHERE id=?", (size, r["id"]))
                    c.execute("UPDATE films SET missing=0 WHERE id=?", (r["film_id"],))
                    continue
                t, y = guess_title_year(p)
                drive = os.path.splitdrive(p)[0] or root
                cur = c.execute("INSERT INTO films(title,year,storage,added_at,updated_at) VALUES(?,?,?,?,?)",
                                (t, y, drive, now, now))
                c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,?)", (cur.lastrowid, p, size))
                added += 1
    # flag missing: files under scanned roots that weren't seen (skipped if the scan was cancelled)
    missing = 0
    for root in ([] if progress.get("cancel") else roots):
        if not os.path.isdir(root): continue
        pref = os.path.join(root, "")
        for r in c.execute("SELECT id, path FROM files WHERE path LIKE ? ESCAPE '\\'",
                           (pref.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",)).fetchall():
            if os.path.normcase(r["path"]) not in seen and not os.path.exists(r["path"]):
                c.execute("UPDATE files SET missing=1 WHERE id=?", (r["id"],)); missing += 1
    c.execute("UPDATE films SET missing = CASE WHEN EXISTS(SELECT 1 FROM files f WHERE f.film_id=films.id AND f.missing=0) THEN 0 ELSE 1 END WHERE EXISTS(SELECT 1 FROM files f WHERE f.film_id=films.id)")
    c.commit()
    return {"added": added, "found": len(seen), "missing_files": missing, "errors": errors[:50]}

# ---------- tags ----------
def get_tag_id(c, name, category=None):
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name: return None
    r = c.execute("SELECT id, category FROM tags WHERE name=?", (name,)).fetchone()
    if r:
        if category and not r["category"]:
            c.execute("UPDATE tags SET category=? WHERE id=?", (category, r["id"]))
        return r["id"]
    if category: c.execute("INSERT OR IGNORE INTO categories(name,sort) VALUES(?,99)", (category,))
    return c.execute("INSERT INTO tags(name,category) VALUES(?,?)", (name, category or None)).lastrowid

def add_tag_if_absent(c, film_id, name, category=None, source="tmdb"):
    """Add an approved tag only if the film doesn't have it yet. Never promotes/removes a pending tag and never
    changes an existing tag's category (used by TMDB genre import)."""
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name: return
    r = c.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    tid = r[0] if r else get_tag_id(c, name, category)
    if c.execute("SELECT 1 FROM film_tags WHERE film_id=? AND tag_id=?", (film_id, tid)).fetchone(): return
    c.execute("INSERT INTO film_tags VALUES(?,?,?,?,?)", (film_id, tid, source, "approved", time.time()))

def add_tag(c, film_id, name, category=None, source="user"):
    tid = get_tag_id(c, name, category)
    if not tid: return
    status = "pending" if source == "ai" else "approved"
    ex = c.execute("SELECT status FROM film_tags WHERE film_id=? AND tag_id=?", (film_id, tid)).fetchone()
    if ex:
        if source != "ai" and ex["status"] == "pending":
            c.execute("UPDATE film_tags SET status='approved' WHERE film_id=? AND tag_id=?", (film_id, tid))
        return
    c.execute("INSERT INTO film_tags VALUES(?,?,?,?,?)", (film_id, tid, source, status, time.time()))

def export_queue(c, film_ids=None):
    out = []
    if film_ids is None: rows = c.execute("SELECT f.* FROM smart_queue q JOIN films f ON f.id=q.film_id ORDER BY q.added_at").fetchall()
    else: rows = [r for i in film_ids for r in c.execute("SELECT * FROM films WHERE id=?", (i,))]
    for r in rows:
        tags = [dict(name=t["name"], category=t["category"], status=t["status"]) for t in c.execute(
            "SELECT t.name, t.category, ft.status FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=?", (r["id"],))]
        paths = [x["path"] for x in c.execute("SELECT path FROM files WHERE film_id=?", (r["id"],))]
        out.append(dict(id=r["id"], title=r["title"], year=r["year"], path=paths[0] if paths else None,
                        paths=paths, director=r["director"], plot=r["plot"], existing_tags=tags))
    return out

def import_ai_tags(c, items, dequeue=True):
    n = 0; skipped = []
    for it in items:
        fid = it.get("film_id", it.get("id"))
        if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone():
            skipped.append(fid); continue
        for t in it.get("tags", []):
            if isinstance(t, str): t = {"name": t}
            before = c.total_changes
            add_tag(c, fid, t.get("name"), t.get("category"), source="ai")
            n += c.total_changes > before
        if dequeue: c.execute("DELETE FROM smart_queue WHERE film_id=?", (fid,))
    c.commit()
    return {"tags_added": n, "skipped_film_ids": skipped}
