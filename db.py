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
CREATE TABLE IF NOT EXISTS aznude_queue(film_id INTEGER PRIMARY KEY REFERENCES films(id) ON DELETE CASCADE, added_at REAL);
CREATE TABLE IF NOT EXISTS saved_searches(id INTEGER PRIMARY KEY, name TEXT, query TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS ix_films_title ON films(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS ix_films_year ON films(year);
CREATE TABLE IF NOT EXISTS sub_probe(path TEXT PRIMARY KEY COLLATE NOCASE, size INTEGER, mtime INTEGER, streams TEXT);
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

# ---------- performer aliases: people.name + people.aliases (+ merges) form one identity ----------
ALIAS_SEP = "\x1f"
_AG = {"key": None, "groups": {}}

def _alias_ok(a):
    """Aliases used to link credits: at least two words (single first names like 'Anna' would match strangers)."""
    return isinstance(a, str) and len(a.split()) >= 2

def alias_groups(c):
    """fold(name) -> sorted list of every name of that person (name, multi-word aliases, merged people). Cached."""
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(people)")}
        if "aliases" not in cols: return {}
        mi = "merged_into" if "merged_into" in cols else "NULL"
        key = tuple(c.execute(f"SELECT COUNT(*), MAX(id), SUM(LENGTH(COALESCE(aliases,''))), SUM(COALESCE({mi},0)) FROM people").fetchone())
        if _AG["key"] == key: return _AG["groups"]
        rows = c.execute(f"SELECT id, name, aliases, {mi} FROM people WHERE aliases IS NOT NULL OR {mi} IS NOT NULL").fetchall()
        extra = {}
        if any(r[3] for r in rows):
            ids = {r[3] for r in rows if r[3]}
            extra = {r[0]: r[1] for r in c.execute(f"SELECT id, name FROM people WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
    except Exception:
        return {}
    parent, disp = {}, {}
    def find(x):
        while parent.setdefault(x, x) != x: parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def node(n):
        n = " ".join(n.split()); k = fold(n); disp.setdefault(k, n); find(k); return k
    for pid, name, als, into in rows:
        if not name: continue
        a = node(name)
        try: lst = json.loads(als or "[]")
        except Exception: lst = []
        for x in lst:
            if _alias_ok(x): parent[find(node(x))] = find(a)
        if into and extra.get(into): parent[find(a)] = find(node(extra[into]))
    members = {}
    for k in parent: members.setdefault(find(k), []).append(k)
    groups = {}
    for ks in members.values():
        if len(ks) < 2: continue
        names = sorted({disp[k] for k in ks}, key=fold)
        for k in ks: groups[k] = names
    _AG.update(key=key, groups=groups)
    return groups

def expand_person(c, name):
    """Every name the person `name` is known by (itself first)."""
    n = " ".join((name or "").split())
    g = alias_groups(c).get(fold(n))
    return [n] + [x for x in g if fold(x) != fold(n)] if g else [n]

def search_alias_names(c, text):
    """Names of every aliased person where some name/alias contains all words of `text` (accent/case-insensitive)."""
    ws = fold(text).split()
    if not ws or len("".join(ws)) < 3: return []
    out = []
    for k, names in alias_groups(c).items():
        if all(w in k for w in ws): out += names
    return sorted(set(out), key=fold)

def alias_nrm_map(c):
    """For ThePornDB matching: alnum-lowercase name -> alnum-lowercase other names of the same person."""
    nr = lambda s: re.sub(r"[^a-z0-9]", "", fold(s))
    m = {}
    for names in alias_groups(c).values():
        ns = {nr(x) for x in names if nr(x)}
        for x in ns: m[x] = ns - {x}
    return m

def person_in_any(field, names):
    """1 if any comma-separated entry of `field` equals any of the ALIAS_SEP-joined `names`."""
    if not field or not names: return 0
    want = {norm_person(x) for x in names.split(ALIAS_SEP)}
    return 1 if any(norm_person(x) in want for x in field.split(",")) else 0

# ---------- studio aliases: hidden spellings (e.g. 'angels.love') -> one main name ('Angels Love') ----------
def studio_key(s):
    """Case-, accent- and punctuation-insensitive studio key: 'angels.love' == 'Angels Love' == 'ANGELS-LOVE'."""
    return re.sub(r"[^0-9a-z]", "", fold(s or ""))

_SA = {"key": None, "map": {}}

def studio_alias_map(c=None):
    """studio_key(alias) -> canonical name. Cached; reloaded when the table changes."""
    own = c is None
    if own: c = connect()
    try:
        try: key = tuple(c.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(alias_key)+LENGTH(canonical)),0) FROM studio_aliases").fetchone())
        except sqlite3.Error: return {}
        if _SA["key"] != key:
            _SA.update(key=key, map={r[0]: r[1] for r in c.execute("SELECT alias_key, canonical FROM studio_aliases")})
        return _SA["map"]
    finally:
        if own: c.close()

def canon_studio(v, c=None):
    """Map every entry of a comma-separated studio value to its main name (unknown names unchanged)."""
    if not v or not isinstance(v, str): return v
    m = studio_alias_map(c)
    if not m: return v
    parts = [x for x in v.split(",") if x.strip()]
    if not any(studio_key(x) in m for x in parts): return v  # no alias involved: leave the value exactly as it is
    out = [m.get(studio_key(x), x.strip()) for x in parts]
    return ", ".join(dict.fromkeys(out)) or None

def studio_names(name, c=None):
    """A studio's main name followed by all its aliases (for search and ThePornDB matching)."""
    m = studio_alias_map(c); k = studio_key(name)
    main = m.get(k, name.strip() if name else name)
    mk = studio_key(main)
    rows = [main]
    if c is None: c2 = connect()
    else: c2 = c
    try: rows += [r[0] for r in c2.execute("SELECT alias FROM studio_aliases WHERE canonical=? OR alias_key=?", (main, mk))]
    except sqlite3.Error: pass
    finally:
        if c is None: c2.close()
    return list(dict.fromkeys(x for x in rows if x))

def studio_key_in(value, keys):
    """True when any entry of a comma-separated studio value has one of the '|'-joined studio keys."""
    ks = set((keys or "").split("|")) - {""}
    return 1 if value and any(studio_key(x) in ks for x in value.split(",")) else 0

def folder_studio(path, roots):
    """The <Studio> folder name of <scene root>/<Studio>/... (None for root-level files and '_' folders)."""
    for r in roots:
        if under(path, r):
            parts = re.split(r"[\\/]", path[len(r.rstrip("\\/")) + 1:])
            if len(parts) > 1 and parts[0] and not parts[0].startswith("_"): return parts[0]
    return None

def path_studio_key(path, roots):
    """studio_key of a file's <Studio> folder under any of the '|'-joined scene roots ('' if none)."""
    f = folder_studio(path or "", [r for r in (roots or "").split("|") if r])
    return studio_key(f) if f else ""

def fill_folder_studios(c):
    """Add-only: scenes with a blank studio get it from their <scene root>/<Studio> folder (hand-edited studios untouched)."""
    roots = scene_roots(c); n = 0
    if not roots: return 0
    for fid, path, ef in c.execute("""SELECT f.id, fi.path, f.edited_fields FROM films f JOIN files fi ON fi.film_id=f.id
                                      WHERE f.kind='scene' AND COALESCE(TRIM(f.studio),'')='' ORDER BY fi.id""").fetchall():
        if ef and "studio" in ef: continue
        st = folder_studio(path, roots)
        if st and c.execute("UPDATE films SET studio=?, updated_at=? WHERE id=? AND COALESCE(TRIM(studio),'')=''",
                            (canon_studio(st, c), time.time(), fid)).rowcount: n += 1
    c.commit(); return n

def studio_alias_search(text, c=None):
    """Main names of studios with an alias whose key contains the search text's key (e.g. 'angels.love')."""
    k = studio_key(text)
    if len(k) < 3: return []
    return sorted({main for ak, main in studio_alias_map(c).items() if k in ak or k in studio_key(main)}, key=fold)

def add_studio_alias(c, alias, canonical, apply=True):
    """Make `alias` a hidden spelling of `canonical`; optionally rewrite existing films. Returns films updated."""
    import time as _t
    alias, canonical = " ".join((alias or "").split()), " ".join((canonical or "").split())
    ak, ck = studio_key(alias), studio_key(canonical)
    if not ak or not ck: raise ValueError("Both names are required")
    if alias == canonical: raise ValueError("Those are the same studio name")
    if ak != ck: c.execute("DELETE FROM studio_aliases WHERE alias_key=?", (ck,))  # the new main name is no longer an alias
    c.execute("UPDATE studio_aliases SET canonical=? WHERE canonical=? OR alias_key IN (SELECT alias_key FROM studio_aliases WHERE canonical=?)", (canonical, alias, alias))
    c.execute("INSERT OR REPLACE INTO studio_aliases(alias_key, alias, canonical) VALUES(?,?,?)", (ak, alias, canonical))
    _SA["key"] = None
    n = 0
    if apply:
        now = _t.time()  # numeric, like every other updated_at
        for fid, st in c.execute("SELECT id, studio FROM films WHERE studio IS NOT NULL AND studio<>''").fetchall():
            new = canon_studio(st, c)
            if new != st:
                c.execute("UPDATE films SET studio=?, updated_at=? WHERE id=?", (new, now, fid)); n += 1
    c.commit()
    return n

def remove_studio_alias(c, alias):
    """Forget a studio alias (films keep their current main name)."""
    c.execute("DELETE FROM studio_aliases WHERE alias_key=?", (studio_key(alias),)); c.commit(); _SA["key"] = None

# ---------- learned ViperGirls rules (Find Gallery): learned only from thread URLs Kyle pastes by hand ----------
VG_SCHEMA = ("CREATE TABLE IF NOT EXISTS vg_learned(studio_key TEXT PRIMARY KEY, studio TEXT, prefix TEXT, prefix_label TEXT, "
             "names_in_title INTEGER, notes TEXT, source_url TEXT, created REAL)")

def vg_learned_list(c):
    c.execute(VG_SCHEMA)
    return [dict(r) for r in c.execute("SELECT * FROM vg_learned ORDER BY studio COLLATE NOCASE")]

def vg_learned_get(c, names):
    """The learned rule for a studio (any of its names/aliases, case and punctuation ignored), or None."""
    c.execute(VG_SCHEMA)
    for n in names or []:
        k = studio_key(n)
        if not k: continue
        r = c.execute("SELECT * FROM vg_learned WHERE studio_key=?", (k,)).fetchone()
        if r: return dict(r)
    return None

def vg_learned_set(c, studio, prefix, prefix_label, names_in_title, notes, source_url):
    """Store (replace) the rule for `studio`: the latest hand-pasted URL always wins."""
    import time as _t
    k = studio_key(studio)
    if not k: raise ValueError("studio required")
    c.execute(VG_SCHEMA)
    c.execute("INSERT OR REPLACE INTO vg_learned(studio_key, studio, prefix, prefix_label, names_in_title, notes, source_url, created) "
              "VALUES(?,?,?,?,?,?,?,?)", (k, " ".join(studio.split()), prefix, prefix_label,
                                          None if names_in_title is None else int(bool(names_in_title)), notes, source_url, _t.time()))
    c.commit()

def vg_learned_delete(c, key):
    c.execute(VG_SCHEMA); n = c.execute("DELETE FROM vg_learned WHERE studio_key=?", (studio_key(key),)).rowcount; c.commit(); return n

# ---------- related studios: Kyle-defined 'sub-label of' links (e.g. 'Out of the Family' -> "Devil's Film") ----------
def _links_table(c):
    c.execute("CREATE TABLE IF NOT EXISTS studio_links(child_key TEXT PRIMARY KEY, child TEXT NOT NULL, parent TEXT NOT NULL)")

def studio_links(c=None):
    own = c is None
    if own: c = connect()
    try:
        _links_table(c)
        return [dict(child=r[0], parent=r[1]) for r in c.execute("SELECT child, parent FROM studio_links ORDER BY parent COLLATE NOCASE, child COLLATE NOCASE")]
    finally:
        if own: c.close()

def related_studios(name, c=None):
    """Studios linked to `name` as sub-label or parent (one level each way), for ThePornDB matching only."""
    if not name: return []
    keys = {studio_key(n) for n in studio_names(name, c)}
    out = []
    for l in studio_links(c):
        if studio_key(l["parent"]) in keys: out.append(l["child"])
        if studio_key(l["child"]) in keys: out.append(l["parent"])
    return list(dict.fromkeys(out))

def add_studio_link(c, child, parent):
    child, parent = " ".join((child or "").split()), " ".join((parent or "").split())
    if not studio_key(child) or not studio_key(parent): raise ValueError("Both studio names are required")
    if studio_key(child) == studio_key(parent): raise ValueError("Those are the same studio")
    _links_table(c)
    c.execute("INSERT OR REPLACE INTO studio_links(child_key, child, parent) VALUES(?,?,?)", (studio_key(child), child, parent)); c.commit()

def remove_studio_link(c, child):
    _links_table(c); c.execute("DELETE FROM studio_links WHERE child_key=?", (studio_key(child),)); c.commit()

def tag_norm(s):
    """Spelling-tolerant tag key: casefold, strip accents, drop spaces/hyphens/underscores, and drop one trailing
    's' when at least 4 chars remain. 'Cum play' == 'cumplay' == 'Cum-Play'; 'Cumshots' == 'Cumshot';
    'Ass' stays 'ass'. Other punctuation is kept, so 'Cum (Kink)' != 'Cum'."""
    k = re.sub(r"[\s\-_]+", "", fold(s or ""))
    return k[:-1] if k.endswith("s") and len(k) - 1 >= 4 else k

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
    c.create_function("tag_norm", 1, tag_norm, deterministic=True)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    c.execute("CREATE TABLE IF NOT EXISTS excluded(id INTEGER PRIMARY KEY, path TEXT, size INTEGER, oshash TEXT, title TEXT, kind TEXT, removed_at REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_excluded_size ON excluded(size)")
    xcols = {r[1] for r in c.execute("PRAGMA table_info(excluded)")}
    for col in ("studio", "cast_", "release_date"):  # shown and sorted in Settings > Removed items
        if col not in xcols: c.execute(f"ALTER TABLE excluded ADD COLUMN {col} TEXT")
    cols = {r[1] for r in c.execute("PRAGMA table_info(films)")}
    if "edited_fields" not in cols: c.execute("ALTER TABLE films ADD COLUMN edited_fields TEXT")
    if "tmdb_status" not in cols: c.execute("ALTER TABLE films ADD COLUMN tmdb_status TEXT")
    if "mpaa" not in cols: c.execute("ALTER TABLE films ADD COLUMN mpaa TEXT")
    if "mpaa_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN mpaa_checked INTEGER")
    for col in ("original_title", "alt_titles", "user_akas"):  # nullable; existing rows untouched
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} TEXT")
    c.create_function("title_match", 4, title_match, deterministic=True)
    if "custom_poster" not in cols: c.execute("ALTER TABLE films ADD COLUMN custom_poster TEXT")  # user art; TMDB never touches it
    for col, typ in (("poster_focus_x", "REAL"), ("poster_focus_src", "TEXT"), ("poster_focus_method", "TEXT"), ("poster_wide", "TEXT")):  # smart crop of wide scene covers
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} {typ}")
    for col in PEOPLE_COLS:  # extra crew roles (nullable; existing rows untouched)
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} TEXT")
    c.create_function("person_in", 2, person_in, deterministic=True)
    c.create_function("person_in_any", 2, person_in_any, deterministic=True)
    c.create_function("fold_in", 2, fold_in, deterministic=True)
    c.create_function("studio_key_in", 2, studio_key_in, deterministic=True)
    c.create_function("path_studio_key", 2, path_studio_key, deterministic=True)
    if "country" not in cols: c.execute("ALTER TABLE films ADD COLUMN country TEXT")  # comma-separated country names
    if "country_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN country_checked INTEGER")  # backfill tried
    if "studio" not in cols: c.execute("ALTER TABLE films ADD COLUMN studio TEXT")  # comma-separated production company names
    if "studio_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN studio_checked INTEGER")  # backfill tried
    if "gallery_path" not in cols: c.execute("ALTER TABLE films ADD COLUMN gallery_path TEXT")  # scene photo gallery folder (gallery.py)
    if "gallery_count" not in cols: c.execute("ALTER TABLE films ADD COLUMN gallery_count INTEGER")
    c.execute("CREATE TABLE IF NOT EXISTS studio_aliases(alias_key TEXT PRIMARY KEY, alias TEXT NOT NULL, canonical TEXT NOT NULL)")  # hidden studio spellings
    c.execute(VG_SCHEMA)  # learned ViperGirls rules (Find Gallery)
    for col, typ in (("resolution", "TEXT"), ("res_width", "INTEGER"), ("res_height", "INTEGER"), ("res_checked", "INTEGER")):
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} {typ}")  # nullable; existing rows untouched
    if "primary_genre" not in cols: c.execute("ALTER TABLE films ADD COLUMN primary_genre TEXT")  # single main genre; hand edits protected
    for col, typ in (("aznude_status", "TEXT"), ("aznude_checked", "REAL"), ("aznude_url", "TEXT"), ("aznude_note", "TEXT")):
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} {typ}")  # AZNude Status; NULL status = Unchecked
    if "tmdb_genres" not in cols: c.execute("ALTER TABLE films ADD COLUMN tmdb_genres TEXT")  # JSON list of TMDB genre names, in TMDB order
    # Subtitles: text summary, manual-override flag (scans never overwrite while set), last-detected marker
    for col, typ in (("subtitles", "TEXT"), ("subs_manual", "INTEGER DEFAULT 0"), ("subs_checked", "REAL")):
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} {typ}")  # existing rows untouched (NULL/0)
    # Scenes libraries: kind='scene' rows never use TMDB; release_date YYYY-MM-DD; tpdb_id = ThePornDB scene id
    if "tpdb_checked" not in cols: c.execute("ALTER TABLE films ADD COLUMN tpdb_checked REAL")
    for col in ("kind", "release_date", "tpdb_id"):
        if col not in cols: c.execute(f"ALTER TABLE films ADD COLUMN {col} TEXT")
    import subtitles as _subs
    c.create_function("sub_lang_in", 2, _subs.lang_in, deterministic=True)
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

# ---------- removed items: never re-added by scans (files are not touched) ----------
def file_hash(p):
    try:
        import tpdb
        return tpdb.oshash(p)
    except Exception: return None

def exclude_film(c, fid):
    """Remember a film's files before it is removed from the library, so later scans skip them (also after a rename/move)."""
    f = c.execute("SELECT title, kind, studio, cast_, release_date FROM films WHERE id=?", (fid,)).fetchone()
    if not f: return 0
    n = 0
    for x in c.execute("SELECT path, size FROM files WHERE film_id=?", (fid,)).fetchall():
        size = x["size"]
        try: size = os.path.getsize(x["path"])
        except OSError: pass
        c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at,studio,cast_,release_date) VALUES(?,?,?,?,?,?,?,?,?)",
                  (x["path"], size, file_hash(x["path"]) if os.path.exists(x["path"]) else None, f["title"], f["kind"], time.time(),
                   f["studio"], f["cast_"], f["release_date"])); n += 1
    c.execute("DELETE FROM files WHERE film_id=?", (fid,))
    c.execute("DELETE FROM films WHERE id=?", (fid,))
    return n

def is_excluded(c, p, size):
    """Same path, same file name + size (moved), or same size + OpenSubtitles hash (renamed or moved copy)."""
    if c.execute("SELECT 1 FROM excluded WHERE path=? COLLATE NOCASE", (p,)).fetchone(): return True
    if size:
        rows = c.execute("SELECT path, oshash FROM excluded WHERE size=?", (size,)).fetchall()
        name = os.path.basename(p).lower()
        if any(os.path.basename((r[0] or "").replace("\\", "/")).lower() == name for r in rows): return True  # moved: same file name + size
        hs = [r[1] for r in rows if r[1]]
        if hs and file_hash(p) in hs: return True
    return False

# ---------- scanning (read-only) ----------
def scene_roots(c):
    return [r.rstrip("\\/") for r in (get_setting(c, "scene_roots", []) or []) if r and r.strip()]

def same_drive(a, b):
    """True when both paths are on the same drive/share (moves never cross drives)."""
    da, dbb = os.path.splitdrive(os.path.abspath(a))[0], os.path.splitdrive(os.path.abspath(b))[0]
    return os.path.normcase(da) == os.path.normcase(dbb)

def lib_prefixes(c, lib):
    """A library filter value -> path prefixes. All scene roots form ONE library: picking any scene root (or a
    studio folder under one) matches the same place under every scene root."""
    lib = (lib or "").strip().rstrip("\\/")
    if not lib: return []
    for sr in scene_roots(c):
        if os.path.normcase(lib) == os.path.normcase(sr): return scene_roots(c)
        if under(lib, sr):
            rel = lib[len(sr) + 1:]
            return [r + ("\\" if "\\" in r or re.match(r"^[A-Za-z]:", r) else "/") + rel for r in scene_roots(c)]
    return [lib]

def under(path, root):
    p, r = os.path.normcase(path), os.path.normcase(root.rstrip("\\/"))
    return p.startswith(r) and len(p) > len(r) and p[len(r)] in "\\/"

def new_scene(c, p, size, root, now):
    """Insert a scene found in a scenes library, prefilled from its filename (studio/date/performers/title)."""
    import tpdb
    n = tpdb.parse_name(p)
    t = tpdb.default_title(n) or os.path.splitext(os.path.basename(p))[0]
    cur = c.execute("INSERT INTO films(title,year,release_date,studio,cast_,storage,kind,primary_genre,added_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (t, int(n["date"][:4]) if n["date"] else None, n["date"], canon_studio(n["studio"] or folder_studio(p, scene_roots(c)), c), ", ".join(n["performers"]) or None,
                     os.path.splitdrive(p)[0] or root, "scene", "Hardcore", now, now))
    c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,?)", (cur.lastrowid, p, size))
    return cur.lastrowid

QUOTE_PAIRS = {'"': '"', "\u201c": "\u201d", "\u201e": "\u201c", "'": "'", "\u2018": "\u2019", "\u00ab": "\u00bb"}

def strip_title_quotes(t):
    """Remove double quotation marks (straight, curly, guillemets) anywhere in a title; apostrophes are kept."""
    if not t: return t
    s = re.sub(r'["\u201c\u201d\u201e\u00ab\u00bb]', '', t)
    return re.sub(r'\s{2,}', ' ', s).strip()

def is_gallery_dir(name):
    import gallery
    return gallery.is_gallery_dir(name)

def scan(c, roots, progress=None):
    progress = progress if progress is not None else {}
    sroots = scene_roots(c)
    now = time.time(); added = 0; seen = set(); errors = []
    for root in roots:
        if not os.path.isdir(root):
            if any(os.path.normcase(root.rstrip("\\/")) == os.path.normcase(sr) for sr in sroots): continue  # scene root not created yet
            errors.append(f"Not a folder or not reachable: {root}"); continue
        for dp, dns, fns in os.walk(root, onerror=lambda e: errors.append(str(e))):
            dns[:] = [d for d in dns if not d.startswith(("$", ".")) and not is_gallery_dir(d)]  # photo galleries are never catalogue items
            if progress.get("cancel"): break
            progress.update(found=len(seen), added=added, current=dp)
            for fn in fns:
                if os.path.splitext(fn)[1].lower() not in VIDEO_EXT: continue
                if re.search(r"\bsample\b", fn, re.I): continue
                p = os.path.join(dp, fn); seen.add(os.path.normcase(p))
                r = c.execute("SELECT id, film_id FROM files WHERE path=?", (p,)).fetchone()
                try: size = os.path.getsize(p)
                except OSError: size = None
                is_scene = any(under(p, sr) for sr in sroots)
                if not r and is_excluded(c, p, size): continue  # removed by hand earlier
                if r:
                    if is_scene: c.execute("UPDATE films SET kind='scene' WHERE id=? AND kind IS NULL", (r["film_id"],))
                    c.execute("UPDATE files SET missing=0, size=? WHERE id=?", (size, r["id"]))
                    c.execute("UPDATE films SET missing=0 WHERE id=?", (r["film_id"],))
                    continue
                if is_scene:
                    new_scene(c, p, size, root, now); added += 1; continue
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
    try: fill_folder_studios(c)  # add-only studio from the <Studio> folder
    except sqlite3.Error as e: errors.append(f"studio from folder: {e}")
    return {"added": added, "found": len(seen), "missing_files": missing, "errors": errors[:50]}

# ---------- tags ----------
def get_tag_id(c, name, category=None, set_category=False):
    """Tag id by name (names are unique, case-insensitive). A missing category is filled in; with set_category=True
    (a category the user explicitly chose) an existing tag is moved to that category, for every film that has it."""
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name: return None
    r = c.execute("SELECT id, category FROM tags WHERE name=?", (name,)).fetchone()
    if r:
        if category and (not r["category"] or (set_category and r["category"] != category)):
            c.execute("INSERT OR IGNORE INTO categories(name,sort) VALUES(?,99)", (category,))
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
    tid = get_tag_id(c, name, category, set_category=(source == "user" and bool(category)))
    if not tid: return
    status = "pending" if source == "ai" else "approved"
    ex = c.execute("SELECT status FROM film_tags WHERE film_id=? AND tag_id=?", (film_id, tid)).fetchone()
    if ex:
        if source != "ai" and ex["status"] == "pending":
            c.execute("UPDATE film_tags SET status='approved' WHERE film_id=? AND tag_id=?", (film_id, tid))
        return
    c.execute("INSERT INTO film_tags VALUES(?,?,?,?,?)", (film_id, tid, source, status, time.time()))

# ---------- primary genre ----------
PG_RE = re.compile(r"^\s*primary[\s_-]*genre\s*[:=]\s*(.+?)\s*$", re.I | re.M)

def genre_tag_name(c, name):
    """Map a genre name to an existing Genre-category tag (case/accent-insensitive); else return the name as given."""
    name = re.sub(r"\s+", " ", (name or "")).strip().strip('"').strip()
    if not name: return None
    k = fold(name)
    for (t,) in c.execute("SELECT name FROM tags WHERE category='Genre' COLLATE NOCASE"):
        if fold(t) == k: return t
    return name

def custom_primary_genres(c):
    """Primary genres the user typed in the edit window that are not Genre tags (settings key custom_primary_genres)."""
    return [x for x in (get_setting(c, "custom_primary_genres", []) or []) if isinstance(x, str) and x.strip()]

def remember_primary_genre(c, name):
    """Normalise a hand-entered primary genre to an existing Genre tag / known custom genre spelling (case/accent-
    insensitive); a brand-new name is added to custom_primary_genres so it shows in the list and filter. Returns the name."""
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name: return None
    k = fold(name)
    for (t,) in c.execute("SELECT name FROM tags WHERE category='Genre' COLLATE NOCASE"):
        if fold(t) == k: return t
    custom = custom_primary_genres(c)
    for t in custom:
        if fold(t) == k: return t
    for (t,) in c.execute("SELECT DISTINCT primary_genre FROM films WHERE COALESCE(primary_genre,'')<>''"):
        if fold(t) == k: name = t; break
    set_setting(c, "custom_primary_genres", custom + [name])
    return name

def pg_user_edited(c, fid):
    r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
    return bool(r) and "primary_genre" in set(json.loads(r[0] or "[]"))

CLASSIC_PORN_SQL = "lower(replace(path,'/','\\')) LIKE '%classic porn%'"
PORN_PRIMARY = ("Hardcore", "Softcore")
SOFTCORE_TAGS = {"softcore", "softcore sex", "pink film", "not hardcore porn"}

def is_classic_porn(c, fid):
    """True if any of the film's files lives under a 'Classic Porn' folder (e.g. D:\\Classic Porn\\)."""
    return c.execute("SELECT 1 FROM files WHERE film_id=? AND " + CLASSIC_PORN_SQL + " LIMIT 1", (fid,)).fetchone() is not None

def porn_primary_genre(c, fid):
    """'Softcore' if the film has a Softcore / Softcore sex / Pink film / Not hardcore porn tag, else 'Hardcore'."""
    names = {(r[0] or "").strip().lower() for r in c.execute(
        "SELECT t.name FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=?", (fid,))}
    return "Softcore" if names & SOFTCORE_TAGS else "Hardcore"

def set_porn_primary_genre(c, fid):
    """Fill an empty, non-hand-edited primary genre of a Classic Porn film with Hardcore/Softcore from its tags."""
    return set_primary_genre(c, fid, porn_primary_genre(c, fid), only_if_empty=True)

def set_primary_genre(c, fid, name, only_if_empty=True):
    """Set films.primary_genre unless the user hand-edited it (and, if only_if_empty, unless it is already set).
    Returns True if the value changed."""
    if name and is_classic_porn(c, fid) and (name or "").strip().lower() not in ("hardcore", "softcore"):
        name = porn_primary_genre(c, fid)  # Classic Porn films: primary genre is only ever Hardcore or Softcore
    name = genre_tag_name(c, name)
    if not name or pg_user_edited(c, fid): return False
    cond = " AND COALESCE(primary_genre,'')=''" if only_if_empty else " AND COALESCE(primary_genre,'')<>?"
    args = [name, fid] + ([] if only_if_empty else [name])
    return c.execute("UPDATE films SET primary_genre=? WHERE id=?" + cond, args).rowcount > 0

def primary_genre_of_item(it):
    """Primary genre from one smart-tag reply item: {"primary_genre": X}, {"Primary genre": X}, a "Primary genre: X"
    line, or a tag/string "Primary genre: X". Returns (genre or None, tags without the primary-genre pseudo-tag)."""
    pg = None
    for k, v in it.items():
        if isinstance(v, str) and re.sub(r"[\s_-]+", "", k).lower() == "primarygenre" and v.strip(): pg = v
    tags = []
    for t in it.get("tags", []) or []:
        nm = t if isinstance(t, str) else (t.get("name") if isinstance(t, dict) else None)
        m = PG_RE.match(nm or "")
        if m: pg = pg or m.group(1); continue
        tags.append(t)
    for k in ("text", "notes", "comment"):
        m = isinstance(it.get(k), str) and PG_RE.search(it[k])
        if m and not pg: pg = m.group(1)
    if isinstance(pg, str): pg = PG_RE.sub(r"\1", pg).strip() or None
    return pg, tags

def backfill_primary_genre(c):
    """Fill empty, non-hand-edited primary_genre from stored TMDB data only (no network):
    1) films.tmdb_genres (TMDB genre list saved by fetches since v1.0.18), else
    2) the film's TMDB-sourced Genre tags in the order TMDB gave them.
    Films with neither are left blank and counted as no_cache."""
    out = dict(filled=0, from_tmdb_genres=0, from_tmdb_tags=0, no_cache=0, no_cache_with_tmdb_id=0, already_set=0, user_edited=0)
    for r in c.execute("SELECT id, tmdb_id, primary_genre, edited_fields, tmdb_genres FROM films ORDER BY id").fetchall():
        if "primary_genre" in set(json.loads(r["edited_fields"] or "[]")): out["user_edited"] += 1; continue
        if (r["primary_genre"] or "").strip(): out["already_set"] += 1; continue
        g, src = None, None
        try: gl = json.loads(r["tmdb_genres"] or "[]")
        except ValueError: gl = []
        if gl: g, src = gl[0], "from_tmdb_genres"
        else:
            t = c.execute("SELECT t.name FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=? AND ft.source='tmdb'"
                          " AND t.category='Genre' COLLATE NOCASE ORDER BY ft.added_at, ft.rowid LIMIT 1", (r["id"],)).fetchone()
            if t: g, src = t[0], "from_tmdb_tags"
        if g and set_primary_genre(c, r["id"], g):
            out["filled"] += 1; out[src] += 1
        else:
            out["no_cache"] += 1
            if r["tmdb_id"]: out["no_cache_with_tmdb_id"] += 1
    c.commit()
    return out

# ---------- AZNude Status (never touches primary_genre or tags) ----------
AZNUDE_STATUSES = ("Hardcore", "Softcore", "Not found")

def norm_aznude_status(v):
    """'hardcore' -> 'Hardcore', 'not found'/'notfound'/'not_found' -> 'Not found'; ''/None/'unchecked' -> None.
    Anything else raises ValueError."""
    if v is None: return None
    k = re.sub(r"[\s_\-]+", "", str(v)).casefold()
    if k in ("", "unchecked", "none", "null"): return None
    for s in AZNUDE_STATUSES:
        if k == s.replace(" ", "").casefold(): return s
    raise ValueError(f"AZNude status must be one of {', '.join(AZNUDE_STATUSES)} or Unchecked, not {v!r}")

def _lines(v, sep=None):
    if not v: return []
    parts = v.split(sep) if sep else v.splitlines()
    return [x for x in (" ".join(p.split()) for p in parts) if x]

def export_aznude_queue(c):
    out = []
    for r in c.execute("SELECT f.* FROM aznude_queue q JOIN films f ON f.id=q.film_id ORDER BY q.added_at, f.id").fetchall():
        paths = [x[0] for x in c.execute("SELECT path FROM files WHERE film_id=? ORDER BY id", (r["id"],))]
        out.append(dict(id=r["id"], title=r["title"], original_title=r["original_title"],
                        alt_titles=_lines(r["alt_titles"], ";"),
                        akas=_lines(r["user_akas"]), year=r["year"], director=r["director"], cast=_lines(r["cast_"], ","),
                        path=paths[0] if paths else None, paths=paths,
                        current_status=r["aznude_status"], current_url=r["aznude_url"]))
    return out

def set_aznude(c, fid, status, url=None, note=None, manual=False, force=False, now=None):
    """Write AZNude fields for one film. Returns 'set', 'protected' (status hand-edited: status kept, url/note
    still filled when given) or 'missing'. manual=True marks aznude_status as hand-edited. Never touches
    primary_genre or tags. Raises ValueError for an unknown status."""
    st = norm_aznude_status(status); now = now or time.time()
    r = c.execute("SELECT edited_fields FROM films WHERE id=?", (fid,)).fetchone()
    if not r: return "missing"
    ed = set(json.loads(r[0] or "[]"))
    url = (url or "").strip() or None; note = (note or "").strip() or None
    if "aznude_status" in ed and not manual and not force:
        if url or note:
            c.execute("UPDATE films SET aznude_url=COALESCE(?,aznude_url), aznude_note=COALESCE(?,aznude_note), updated_at=? WHERE id=?", (url, note, now, fid))
        return "protected"
    if manual: ed.add("aznude_status")
    c.execute("UPDATE films SET aznude_status=?, aznude_checked=?, aznude_url=?, aznude_note=?, edited_fields=?, updated_at=? WHERE id=?",
              (st, now, url, note, json.dumps(sorted(ed)), now, fid))
    return "set"

def import_aznude(c, items, dequeue=True):
    """items: [{film_id, status, url, note}]. Hand-edited statuses are kept. Imported films leave the AZNude queue."""
    res = {"set": 0, "protected": [], "skipped_film_ids": [], "errors": []}
    for it in items:
        fid = it.get("film_id", it.get("id"))
        try: fid = int(fid)
        except (TypeError, ValueError): res["errors"].append(f"bad film_id {fid!r}"); continue
        try: r = set_aznude(c, fid, it.get("status", it.get("aznude_status")), it.get("url"), it.get("note"))
        except ValueError as e: res["errors"].append(f"film {fid}: {e}"); continue
        if r == "missing": res["skipped_film_ids"].append(fid); continue
        if r == "set": res["set"] += 1
        else: res["protected"].append(fid)
        if dequeue: c.execute("DELETE FROM aznude_queue WHERE film_id=?", (fid,))
    c.commit()
    return res

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
    n = 0; skipped = []; pg_set = 0
    for it in items:
        fid = it.get("film_id", it.get("id"))
        if not c.execute("SELECT 1 FROM films WHERE id=?", (fid,)).fetchone():
            skipped.append(fid); continue
        pg, tags = primary_genre_of_item(it)
        if pg and set_primary_genre(c, fid, pg, only_if_empty=False): pg_set += 1
        for t in tags:
            if isinstance(t, str): t = {"name": t}
            before = c.total_changes
            add_tag(c, fid, t.get("name"), t.get("category"), source="ai")
            n += c.total_changes > before
        if dequeue: c.execute("DELETE FROM smart_queue WHERE film_id=?", (fid,))
    c.commit()
    return {"tags_added": n, "skipped_film_ids": skipped, "primary_genres_set": pg_set}
