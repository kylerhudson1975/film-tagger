"""Person pages: library people index, TMDB person resolution/caching, filmography with owned-film matching."""
import os, re, json, time, datetime, threading, unicodedata, urllib.request, urllib.error, urllib.parse
from collections import Counter as Counter_
from flask import request, jsonify, send_from_directory
import db

PEOPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY, tmdb_person_id INTEGER, name TEXT NOT NULL, name_norm TEXT UNIQUE,
  profile_path TEXT, bio TEXT, birthday TEXT, deathday TEXT, place TEXT, fetched_at REAL,
  known_for TEXT, credits_json TEXT, tmdb_manual INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_people_tmdb ON people(tmdb_person_id);
"""
# ---------- height display (Settings -> Height units: metric / imperial / both) ----------
HEIGHT_UNITS = ("both", "metric", "imperial")
_H_CM = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:cm|centimet(?:er|re)s?)?\s*$", re.I)
_H_M = re.compile(r"^\s*(\d(?:[.,]\d+)?)\s*(?:m|met(?:er|re)s?)\s*$", re.I)
_H_FTIN = re.compile(r"^\s*(\d)\s*(?:'|′|’|ft\.?|feet|foot)\s*(?:(\d{1,2}(?:[.,]\d+)?)\s*(?:\"|″|”|''|′′|in\.?|inch(?:es)?)?)?\s*$", re.I)
_H_IN = re.compile(r"^\s*(\d{2}(?:[.,]\d+)?)\s*(?:\"|″|in\.?|inch(?:es)?)\s*$", re.I)

def parse_height_cm(v):
    """Height as stored ('162cm', '162', 162, '1.62 m', '5\'4"', '5 ft 4 in', '64 in') -> centimetres (float) or None."""
    if v is None or isinstance(v, bool): return None
    if isinstance(v, (int, float)): cm = float(v)
    else:
        t = str(v).strip()
        if not t: return None
        m = _H_CM.match(t)
        if m: cm = float(m.group(1).replace(",", "."))
        elif _H_M.match(t): cm = float(_H_M.match(t).group(1).replace(",", ".")) * 100
        elif _H_FTIN.match(t):
            m = _H_FTIN.match(t); cm = (int(m.group(1)) * 12 + float((m.group(2) or "0").replace(",", "."))) * 2.54
        elif _H_IN.match(t): cm = float(_H_IN.match(t).group(1).replace(",", ".")) * 2.54
        else: return None
    return cm if 50 <= cm <= 272 else None

def fmt_height(v, units="both"):
    """Display a height in the chosen units; anything that can't be read as a height is returned unchanged."""
    cm = parse_height_cm(v)
    if cm is None: return v if v is None or isinstance(v, str) else str(v)
    inches = round(cm / 2.54)          # round the total first, so 5' 11.6" becomes 6' 0", never 5' 12"
    ft, inch = divmod(inches, 12)
    imp, met = f"{ft}\u2032{inch}\u2033", f"{round(cm)} cm"
    return met if units == "metric" else imp if units == "imperial" else f"{imp} ({met})"

def height_units(c):
    u = db.get_setting(c, "height_units", "both")
    return u if u in HEIGHT_UNITS else "both"

ROLE_COLS = db.PEOPLE_ROLES  # role -> films column
ROLE_DEPT = {"cast": "Acting", "director": "Directing", "writer": "Writing", "producer": "Production",
             "cinematographer": "Camera", "composer": "Sound"}
DEPT_ORDER = ["Acting", "Directing", "Writing", "Production", "Camera", "Sound", "Editing", "Art", "Costume & Make-Up",
              "Visual Effects", "Crew", "Lighting"]
STALE_S = 14 * 86400
NO_MATCH = -1  # tmdb_person_id value meaning "searched TMDB, nothing found"

def tmdb_title_kind(kind=None, item=None):
    k = kind
    if not k and item is not None:
        k = item.get("media_type") or item.get("tmdb_kind")
    return "tv" if str(k or "").strip().lower() == "tv" else "movie"

def tmdb_page_url(tid, kind="movie"):
    if not tid:
        return None
    return f"https://www.themoviedb.org/{tmdb_title_kind(kind)}/{int(tid)}"

def load_title_credits(tmdb, key, tid, kind="movie"):
    """Film or TV credits for a library title. TV uses aggregate_credits, then credits. Never cross-tries movie/TV ids."""
    kind = tmdb_title_kind(kind)
    paths = [f"tv/{int(tid)}/aggregate_credits", f"tv/{int(tid)}/credits"] if kind == "tv" else [f"movie/{int(tid)}/credits"]
    last = None
    for path in paths:
        try:
            cr = tmdb(path, key) or {}
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise
            last = e
            continue
        # Flatten roles[]/jobs[] so callers can use character/job the same way as movies.
        out = {"cast": [], "crew": []}
        for x in cr.get("cast") or []:
            y = dict(x)
            if not (y.get("character") or "").strip():
                parts, seen = [], set()
                for r in y.get("roles") or []:
                    n = (r.get("character") or "").strip()
                    if n and n not in seen:
                        seen.add(n); parts.append(n)
                y["character"] = " / ".join(parts)
            out["cast"].append(y)
        for x in cr.get("crew") or []:
            jobs = [(r.get("job") or "").strip() for r in (x.get("jobs") or []) if (r.get("job") or "").strip()]
            if not jobs:
                j = (x.get("job") or "").strip()
                jobs = [j] if j else [""]
            seen = set()
            for j in jobs:
                if j in seen: continue
                seen.add(j)
                y = dict(x); y["job"] = j
                out["crew"].append(y)
        return out
    if last is not None:
        raise last
    return {"cast": [], "crew": []}

def _norm_combined_credit(x, typ="cast"):
    """Normalize a person combined_credits row so movie and TV share title/date/media_type fields."""
    x = x or {}
    media = tmdb_title_kind(item=x)
    rec = dict(id=x.get("id"),
               title=x.get("title") or x.get("name"),
               original_title=x.get("original_title") or x.get("original_name"),
               release_date=x.get("release_date") or x.get("first_air_date"),
               poster_path=x.get("poster_path"),
               media_type=media)
    if typ == "cast":
        rec["character"] = (x.get("character") or "").strip()
        if not rec["character"]:
            parts, seen = [], set()
            for r in x.get("roles") or []:
                n = (r.get("character") or "").strip()
                if n and n not in seen:
                    seen.add(n); parts.append(n)
            rec["character"] = " / ".join(parts)
    else:
        rec["job"] = x.get("job")
        rec["department"] = x.get("department")
    return rec
TPDB_MAX_PAGES = 200  # 100 scenes per page (20,000 scenes)
TPDB_NONE = "-"  # tpdb_performer_id value meaning "searched ThePornDB, nothing found"

# ThePornDB profile columns (adult performers). edited_fields = profile fields changed by hand; never overwritten.
TPDB_COLS = [("source", "TEXT"), ("tpdb_performer_id", "TEXT"), ("tpdb_manual", "INTEGER DEFAULT 0"), ("aliases", "TEXT"),
             ("nationality", "TEXT"), ("measurements", "TEXT"), ("height", "TEXT"), ("career", "TEXT"), ("links_json", "TEXT"),
             ("image_url", "TEXT"), ("gender", "TEXT"), ("edited_fields", "TEXT"), ("tpdb_fetched_at", "REAL"), ("photo_file", "TEXT"), ("tpdb_scenes_json", "TEXT"), ("tpdb_scenes_at", "REAL"),
             ("tpdb_scenes_total", "INTEGER"), ("merged_into", "INTEGER"), ("photo_orig", "TEXT"), ("birth_date_source", "TEXT"),
             ("ethnicity", "TEXT"), ("ethnicity_source", "TEXT"), ("ethnicity_checked_at", "REAL"),
             ("eth_name_checked_at", "REAL"), ("eth_ext_checked_at", "REAL"), ("ethnicity_skipped_at", "REAL"),
             ("ethnicity_listings", "TEXT")]
EDITABLE = ("bio", "birthday", "place", "nationality", "measurements", "height", "career", "aliases", "links", "gender", "deathday", "ethnicity")
# Gender values: ThePornDB's wording (what the library already holds) plus 'Unknown' (set by hand: checked, not known).
GENDERS = ("Female", "Male", "Non Binary", "Transgender Female", "Transgender Male", "Unknown")
TMDB_GENDER = {1: "Female", 2: "Male", 3: "Non Binary"}  # TMDB person.gender (0 = not set)
def norm_gender(v):
    """Any spelling (female, non-binary, Trans woman, ...) -> one of GENDERS, else None."""
    k = re.sub(r"[^a-z]", "", str(v or "").lower())
    if not k: return None
    alias = {"f": "Female", "woman": "Female", "m": "Male", "man": "Male", "nonbinary": "Non Binary", "nb": "Non Binary",
             "transwoman": "Transgender Female", "transfemale": "Transgender Female", "transgenderwoman": "Transgender Female",
             "transman": "Transgender Male", "transmale": "Transgender Male", "transgenderman": "Transgender Male"}
    for g in GENDERS:
        if k == re.sub(r"[^a-z]", "", g.lower()): return g
    return alias.get(k)
# Ethnicity: only what a source publishes (ThePornDB performer extras.ethnicity, Wikidata P172 'ethnic group') or a hand
# edit; never inferred from photos, names or nationality (TMDB has no such field). Obvious variants fold into this small
# set; a source value that maps to none of them is kept as published.
ETHNICITIES = ("Asian", "Black", "Jewish", "Latina/Latino", "Middle Eastern", "Mixed", "Native American", "Pacific Islander", "South Asian", "White")
_ETH_ALIAS = {"asian": "Asian", "asianpeople": "Asian", "asians": "Asian", "eastasian": "Asian", "southeastasian": "Asian", "asianamerican": "Asian", "asianamericans": "Asian",
              "black": "Black", "african": "Black", "africanamerican": "Black", "africanamericans": "Black", "ebony": "Black",
              "afroamerican": "Black", "blackpeople": "Black", "blackamericans": "Black", "blackbritish": "Black", "blackbritishpeople": "Black",
              "jewish": "Jewish", "jews": "Jewish", "jewishpeople": "Jewish", "ashkenazijews": "Jewish", "ashkenazi": "Jewish",
              "ashkenazijewish": "Jewish", "sephardijews": "Jewish", "sephardicjews": "Jewish", "sephardi": "Jewish", "sephardic": "Jewish",
              "mizrahijews": "Jewish", "mizrahi": "Jewish", "jewishamericans": "Jewish", "americanjews": "Jewish",
              "britishjews": "Jewish", "israelijews": "Jewish", "jew": "Jewish", "jewishcanadian": "Jewish", "canadianjews": "Jewish",
              "russianjews": "Jewish", "frenchjews": "Jewish", "germanjews": "Jewish", "polishjews": "Jewish", "hungarianjews": "Jewish",
              "latin": "Latina/Latino", "latina": "Latina/Latino", "latino": "Latina/Latino", "latinx": "Latina/Latino", "hispanic": "Latina/Latino",
              "latinalatino": "Latina/Latino", "latinolatina": "Latina/Latino", "hispanicandlatinoamericans": "Latina/Latino",
              "hispanicamericans": "Latina/Latino", "latinoamericans": "Latina/Latino", "hispanics": "Latina/Latino", "latinos": "Latina/Latino",
              "middleeastern": "Middle Eastern", "arab": "Middle Eastern", "arabic": "Middle Eastern", "arabs": "Middle Eastern",
              "mixed": "Mixed", "mixedrace": "Mixed", "multiracial": "Mixed", "biracial": "Mixed", "multiracialamericans": "Mixed",
              "nativeamerican": "Native American", "nativeamericans": "Native American", "americanindian": "Native American",
              "nativeamericansintheunitedstates": "Native American", "pacificislander": "Pacific Islander", "pacificislanders": "Pacific Islander",
              "indian": "South Asian", "southasian": "South Asian", "southasians": "South Asian", "desi": "South Asian",
              "white": "White", "caucasian": "White", "caucasians": "White", "european": "White", "europeanpeople": "White", "europeans": "White", "whitepeople": "White",
              "whiteamericans": "White", "whitebritish": "White", "whitebritishpeople": "White", "europeanamericans": "White"}
# Ancestry / heritage values from Wikidata P172 (and the same wording typed by hand) fold into the main groups.
# Nationality-style labels (French people, Americans, Irish people, …) and a short list Kyle is still deciding
# (Armenians, Spanish Americans, Brazilian Americans, Galicians, Russians, Bahamian Americans, Puerto Ricans,
# Berbers, Métis, Louisiana Creole people, Scandinavians) are left as published. Horn of Africa / East African
# ethnic groups fold to Black, never White.
_ETH_ALIAS.update({
    "japaneseamericans": "Asian", "chineseamericans": "Asian", "filipinoamericans": "Asian",
    "britishchinese": "Asian", "koreanamericans": "Asian", "minangkabau": "Asian",
    "africanbrazilians": "Black", "afrobrazilian": "Black", "afrobrazilians": "Black",
    "somali": "Black", "somalis": "Black", "somalipeople": "Black", "somaliamericans": "Black",
    "somalian": "Black", "somalians": "Black",
    "ethiopian": "Black", "ethiopians": "Black", "ethiopianpeople": "Black", "ethiopianamericans": "Black",
    "eritrean": "Black", "eritreans": "Black", "eritreanpeople": "Black", "eritreanamericans": "Black",
    "nubian": "Black", "nubians": "Black", "nubianpeople": "Black",
    "amhara": "Black", "amharas": "Black", "amharapeople": "Black",
    "oromo": "Black", "oromos": "Black", "oromopeople": "Black",
    "tigray": "Black", "tigrayan": "Black", "tigrayans": "Black", "tigrinya": "Black", "tigrinyapeople": "Black",
    "habesha": "Black", "habeshas": "Black", "afarpeople": "Black", "afar": "Black",
    "djiboutian": "Black", "djiboutians": "Black",
    "statesidepuertoricans": "Latina/Latino", "cubanamericans": "Latina/Latino",
    "dominicanamericans": "Latina/Latino", "mexicanamericans": "Latina/Latino",
    "guatemalanamericans": "Latina/Latino", "honduranamericans": "Latina/Latino",
    "peruvianamericans": "Latina/Latino",
    "lebaneseamericans": "Middle Eastern", "arabmexican": "Middle Eastern", "arabmexicans": "Middle Eastern",
    "lebanesemexicans": "Middle Eastern", "moroccanamericans": "Middle Eastern",
    "moroccancanadians": "Middle Eastern", "palestinians": "Middle Eastern", "palestinian": "Middle Eastern",
    "eurasian": "Mixed", "eurasians": "Mixed",
    "cherokee": "Native American", "dene": "Native American", "cayugapeople": "Native American", "cayuga": "Native American",
    "chipewyanpeople": "Native American", "chipewyan": "Native American", "cree": "Native American",
    "dakotapeople": "Native American", "dakota": "Native American", "menominee": "Native American",
    "mohawk": "Native American", "muscogee": "Native American", "nakota": "Native American",
    "navajo": "Native American", "omahatribeofnebraska": "Native American", "omaha": "Native American",
    "oneida": "Native American", "pawneenation": "Native American", "pawnee": "Native American",
    "purepecha": "Native American", "saulteaux": "Native American",
    "tainopeople": "Native American", "taino": "Native American",
    "tsleilwaututhfirstnation": "Native American", "tsleilwaututh": "Native American",
    "maori": "Pacific Islander", "maoris": "Pacific Islander", "maoripeople": "Pacific Islander",
    "nativehawaiians": "Pacific Islander", "nativehawaiian": "Pacific Islander",
    "ngatihaua": "Pacific Islander",
    "indianamericans": "South Asian", "punjabis": "South Asian", "punjabi": "South Asian",
    "irishamericans": "White", "germanamericans": "White", "italianamericans": "White",
    "dutchamericans": "White", "englishamericans": "White", "scottishamericans": "White",
    "frenchamericans": "White", "norwegianamericans": "White", "polishamericans": "White",
    "swedishamericans": "White", "swissamericans": "White", "greekamericans": "White",
    "finnishamericans": "White", "serbianamericans": "White", "sicilianamericans": "White",
    "albanianamericans": "White", "arbereshe": "White", "azoreancanadians": "White",
    "belgianamericans": "White", "cornishamericans": "White", "cornishcanadians": "White",
    "danishamericans": "White", "dutchcanadians": "White", "germancanadians": "White",
    "irishcanadians": "White", "ladins": "White", "lemkos": "White",
    "luxembourgishcanadians": "White", "portuguesecanadians": "White", "russianamericans": "White",
    "sudetengermans": "White", "ukrainianamericans": "White", "welshamericans": "White",
})
# Groups that count when deciding a single auto-assignment (Mixed can be saved when it is the only mapped
# value, but it does not compete with these nine).
ETH_DECIDE = ("Asian", "Black", "Jewish", "Latina/Latino", "Middle Eastern", "Native American", "Pacific Islander", "South Asian", "White")
_ETH_NONE = {"", "other", "unknown", "na", "none", "notapplicable", "notlisted"}
def _eth_key(v):
    """Lowercased letters only, diacritics stripped (Māori -> maori), for alias lookup."""
    t = " ".join(str(v or "").split())
    t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z]", "", t.lower())
def norm_ethnicity(v, keep=True):
    """A source / hand-typed ethnicity -> one of ETHNICITIES; an unmapped value is kept as written (keep=True) or None.
    'Other' / 'Unknown' / '' -> None (nothing published)."""
    t = " ".join(str(v or "").split())
    k = _eth_key(t)
    if k in _ETH_NONE: return None
    if k in _ETH_ALIAS: return _ETH_ALIAS[k]
    return t[:60] if keep else None
def eth_parts(v):
    """A stored ethnicity -> its values ('Irish Americans, White' -> ['Irish Americans', 'White']; several = several sources' groups)."""
    return [x.strip() for x in str(v or "").split(",") if x.strip()]
def resolve_ethnicity(parts):
    """Fold source labels (Wikidata P172, etc.) into at most one main group.
    If exactly one of ETH_DECIDE is present (unmapped / undecided labels ignored), return that group.
    If two or more of those groups remain, it is a conflict (no auto-assignment; Needs ethnicity review).
    Mixed is saved only when it is the only mapped value. Unmapped-only lists are kept as published.
    Returns (value_or_None, conflict, listings)."""
    listings = list(dict.fromkeys(x.strip() for x in parts if str(x).strip()))
    decisive, mixed = [], False
    for lab in listings:
        n = norm_ethnicity(lab, keep=True)
        if n in ETH_DECIDE:
            if n not in decisive: decisive.append(n)
        elif n == "Mixed":
            mixed = True
    if len(decisive) == 1: return decisive[0], False, listings
    if len(decisive) >= 2: return None, True, listings
    if mixed: return "Mixed", False, listings
    return (", ".join(listings) if listings else None), False, listings
def eth_locked(row):
    """True when ethnicity is a protected hand edit (never overwritten by a fill or ancestry fold)."""
    if (row["ethnicity_source"] or "") in ("manual", "hand edit"): return True
    try: return "ethnicity" in json.loads(row["edited_fields"] or "[]")
    except Exception: return False
def apply_ancestry_fold(c):
    """One pass over unlocked source ethnicities: fold ancestry labels into a main group, or move conflicts
    to the Needs ethnicity queue (listings kept, ethnicity cleared). Hand edits are never touched. Idempotent."""
    folded = conflict = 0
    for r in c.execute("SELECT id, ethnicity, ethnicity_source, edited_fields FROM people WHERE merged_into IS NULL AND COALESCE(ethnicity,'')<>''"):
        if eth_locked(r): continue
        value, is_conflict, listings = resolve_ethnicity(eth_parts(r["ethnicity"]))
        if is_conflict:
            c.execute("UPDATE people SET ethnicity=NULL, ethnicity_listings=? WHERE id=?",
                      (json.dumps(listings, ensure_ascii=False), r["id"]))
            conflict += 1
        elif value and value != r["ethnicity"]:
            c.execute("UPDATE people SET ethnicity=? WHERE id=?", (value, r["id"]))
            folded += 1
    c.commit()
    return folded, conflict
def parse_eth_listings(row):
    """Wikidata P172 labels stored for review, or []."""
    raw = row["ethnicity_listings"] if "ethnicity_listings" in row.keys() else None
    if not raw: return []
    try:
        v = json.loads(raw)
        if isinstance(v, list): return [str(x).strip() for x in v if str(x).strip()]
    except Exception:
        pass
    return eth_parts(raw)
ADULT_HINT = re.compile(r"porn|xxx|adult", re.I)
COCREDIT = {}  # "ids": fn(conn, [person ids]) -> library film ids credited to all of them (app.build_query cocredit=)
RLBL_CO = {"director": "Director", "cast": "Actor", "writer": "Writer", "producer": "Producer", "cinematographer": "Cinematographer", "composer": "Composer"}

# Favorites (star on the person page): favorite=1 and when it was starred. Only ever changed by the star toggle.
FAV_COLS = [("favorite", "INTEGER DEFAULT 0"), ("favorite_at", "REAL")]
# Performer photo gallery (persongallery.py): a folder of the user's own images, linked in place. Only the gallery endpoints change it.
GALLERY_COLS = [("gallery_dir", "TEXT")]

def site_label(site):
    """ThePornDB parent / network name of a scene's site (e.g. 'Taboo Heat' -> 'Cory Chase Network'), or None."""
    if not isinstance(site, dict): return None
    for k in ("parent", "network"):
        v = site.get(k)
        if isinstance(v, dict) and (v.get("name") or "").strip() and (v.get("name") or "").strip() != (site.get("name") or "").strip():
            return v["name"].strip()
    return None

GROUP_MIN = 3  # Built profiles 'Group by': a performer is listed under a studio with at least this many of her scenes (her top studio always)

def migrate(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(people)")}
    for col, typ in TPDB_COLS + FAV_COLS + GALLERY_COLS:  # nullable / default 0: existing rows untouched
        if col not in have: c.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")
    c.execute(REVIEW_SCHEMA)
    import persongallery as _pg
    c.execute(_pg.HIDE_SCHEMA)  # hidden photos in a performer gallery (files are never moved)
    c.commit()

# Classic Porn people whose TMDB person could not be picked confidently (several candidates): listed, never guessed
REVIEW_SCHEMA = ("CREATE TABLE IF NOT EXISTS tmdb_match_review(person_id INTEGER PRIMARY KEY, name TEXT, candidates_json TEXT, "
                 "reason TEXT, at REAL)")

def clean_value(k, v):
    """Normalise a hand-entered profile field for storage."""
    if k == "aliases":
        l = [a.strip() for a in (v if isinstance(v, list) else str(v or "").split(",")) if a.strip()]
        return json.dumps(l) if l else None
    if k == "links":
        if isinstance(v, str):  # "Label: URL" or bare URL per line
            d = {}
            for line in v.splitlines():
                line = line.strip()
                if not line: continue
                m = re.match(r"([^:]{1,40}):\s*(https?://\S+)$", line)
                lab, url = (m.group(1).strip(), m.group(2)) if m else (None, line)
                if not url.startswith("http"): continue
                if not lab:
                    host = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
                    lab = {"iafd.com": "IAFD", "x.com": "X", "twitter.com": "Twitter", "instagram.com": "Instagram",
                           "onlyfans.com": "OnlyFans", "tiktok.com": "TikTok"}.get(host, host)
                d[lab] = url
            v = d
        v = {str(a): str(b) for a, b in (v or {}).items() if str(b).startswith("http")}
        return json.dumps(v) if v else None
    if k == "gender": return norm_gender(v)
    if k == "ethnicity": return ", ".join(dict.fromkeys(x for x in map(norm_ethnicity, eth_parts(v)) if x)) or None
    if k == "deathday":  # YYYY-MM-DD only; anything else clears it
        m = re.match(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", str(v or ""))
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    return (str(v).strip() or None) if v is not None else None

def adult_roots(c):
    """Scene libraries plus adult film libraries (setting adult_roots, else library folders named porn/xxx/adult)."""
    extra = db.get_setting(c, "adult_roots", None)
    if extra is None: extra = [r for r in (db.get_setting(c, "roots", []) or []) if r and ADULT_HINT.search(os.path.basename(r.rstrip("\\/")) or r)]
    return [r.rstrip("\\/") for r in list(db.scene_roots(c)) + list(extra or []) if r]

_AFI = {"gen": None, "ids": None}
def adult_film_ids(c):
    """Ids of adult titles (scenes + files under adult roots). Inside db.batch_cache() computed once per batch."""
    if db._BATCH[0] and _AFI["gen"] == db.BATCH_GEN[0]: return _AFI["ids"]
    ids = _adult_film_ids(c)
    if db._BATCH[0]: _AFI.update(gen=db.BATCH_GEN[0], ids=ids)
    return ids
def _adult_film_ids(c):
    roots = adult_roots(c)
    ids = {r[0] for r in c.execute("SELECT id FROM films WHERE kind='scene'")}
    if roots:
        for fid, path in c.execute("SELECT film_id, path FROM files"):
            if any(db.under(path, r) for r in roots): ids.add(fid)
    return ids

def classic_film_ids(c):
    """Classic Porn titles: files under a 'Classic Porn' folder (db.CLASSIC_PORN_SQL), plus feature films (not scenes) whose
    primary genre is Hardcore (the Dashboard's Classic Porn)."""
    ids = {r[0] for r in c.execute("SELECT film_id FROM files WHERE " + db.CLASSIC_PORN_SQL)}
    ids |= {r[0] for r in c.execute("SELECT id FROM films WHERE COALESCE(kind,'')<>'scene' AND primary_genre='Hardcore'")}
    return ids

def photo_choice(row):
    """Which stored photo a person shows: ('tpdb', url) or ('tmdb', profile_path), or None. ThePornDB-primary people
    (source 'tpdb': scene performers) use the ThePornDB image first; everyone else (TMDB primary, incl. anyone in Classic
    Porn) uses the TMDB photo first and the ThePornDB image only when TMDB has none."""
    if not row: return None
    tp, tm = row["image_url"], row["profile_path"]
    if row["source"] == "tpdb": return ("tpdb", tp) if tp else (("tmdb", tm) if tm else None)
    return ("tmdb", tm) if tm else (("tpdb", tp) if tp else None)

def review_film_ids(c):
    return {r[0] for r in c.execute("SELECT id FROM films WHERE COALESCE(notes,'') LIKE '%Review by hand%'")}

def parse_tpdb_performer(v):
    """id, slug or https://theporndb.net/performers/<id-or-slug> -> identifier, else None."""
    v = (v or "").strip()
    m = re.search(r"/performers/([A-Za-z0-9._-]+)", v)
    if m: return m.group(1)
    return v if re.fullmatch(r"[A-Za-z0-9._-]+", v) else None

def pick_performer(results, name):
    """Exact (accent/case-insensitive) name match first, then alias match; ties: most scenes/rating, then API order."""
    t = db.fold(" ".join(name.split()))
    def nm(x): return db.fold(" ".join((x or "").split()))
    exact = [p for p in results if nm(p.get("name")) == t]
    if not exact: exact = [p for p in results if any(nm(a) == t for a in (p.get("aliases") or []))]
    if not exact: return None
    return exact[0] if len(exact) == 1 else max(exact, key=lambda p: ((p.get("scenes_count") or 0), (p.get("rating") or 0)))

def tpdb_fields(p):
    """ThePornDB performer resource -> people columns. IAFD is stored only if ThePornDB provides it."""
    ex = p.get("extras") or {}
    cs, ce = ex.get("career_start_year"), ex.get("career_end_year")
    career = (f"{cs}–{ce}" if cs and ce else f"{cs}–present" if cs else f"until {ce}" if ce else None)
    links = {k: v for k, v in (ex.get("links") or {}).items() if isinstance(v, str) and v.startswith("http")}
    return dict(bio=(p.get("bio") or "").strip() or None, birthday=ex.get("birthday") or None, deathday=ex.get("deathday") or None,
                place=ex.get("birthplace") or None, nationality=ex.get("nationality") or None,
                measurements=ex.get("measurements") or None, height=ex.get("height") or None, career=career,
                aliases=json.dumps([a for a in (p.get("aliases") or []) if a]) if p.get("aliases") else None,
                links_json=json.dumps(links) if links else None, image_url=p.get("image") or p.get("thumbnail") or None,
                gender=norm_gender(ex.get("gender")), ethnicity=norm_ethnicity(ex.get("ethnicity")))

def photo_locked(row):
    """A profile photo Kyle set by hand (upload, URL, crop in the Person Profile edit window) is a protected hand edit
    ('photo' in edited_fields, or a manual-* photo file): no automatic job changes that person's photo sources."""
    if not row: return False
    try: ed = set(json.loads(row["edited_fields"] or "[]"))
    except Exception: ed = set()
    return "photo" in ed or (row["photo_file"] or "").startswith("manual-")

PHOTO_AUTO = ("image_url", "profile_path", "photo_file", "photo_orig")  # columns automatic writers may never touch on a protected person

def apply_tpdb(c, row, p, allow_image=True, fill_only=False):
    """Write a ThePornDB performer onto a people row, skipping hand-edited fields. fill_only (TMDB-primary people, e.g.
    anyone in Classic Porn): only fill fields that are empty and keep the TMDB source; the ThePornDB image is stored as a
    fallback photo (used only when TMDB has none)."""
    f = tpdb_fields(p); edited = set(json.loads(row["edited_fields"] or "[]"))
    if not allow_image: f["image_url"] = None
    sets = {k: v for k, v in f.items() if k not in edited and not (k in PHOTO_AUTO and photo_locked(row))}
    if fill_only: sets = {k: v for k, v in sets.items() if v and (k == "image_url" or not row[k])}
    if not sets.get("gender") or row["gender"]: sets.pop("gender", None)  # gender is add-only: never replaces a stored value
    if not sets.get("deathday") or row["deathday"]: sets.pop("deathday", None)  # death date is add-only
    if not sets.get("ethnicity") or row["ethnicity"]: sets.pop("ethnicity", None)  # ethnicity is add-only, never over a hand edit
    else: sets["ethnicity_source"] = f"theporndb {p.get('id')}"
    if not sets.get("birthday"): sets.pop("birthday", None)  # ThePornDB has none: keep one found elsewhere (Wikidata, TMDB)
    elif sets["birthday"] != row["birthday"]: sets["birth_date_source"] = f"theporndb {p.get('id')}"
    if fill_only: sets.update(tpdb_performer_id=str(p.get("id")), tpdb_fetched_at=time.time())
    else: sets.update(source="tpdb", tpdb_performer_id=str(p.get("id")), tpdb_fetched_at=time.time(), fetched_at=time.time())
    c.execute("UPDATE people SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", list(sets.values()) + [row["id"]]); c.commit()

def tpdb_resolve(c, token, row, call):
    """Search /performers for the row's name; returns the full performer dict or None. `call(path, **params)`."""
    if row["tpdb_performer_id"] and row["tpdb_performer_id"] != TPDB_NONE:
        return call(f"/performers/{row['tpdb_performer_id']}").get("data") or None
    hit = pick_performer(call("/performers", q=row["name"]).get("data") or [], row["name"])
    if not hit: return None
    return call(f"/performers/{hit['id']}").get("data") or hit

def tpdb_update(c, token, row, call, allow_image=True, fill_only=False):
    """Resolve + apply. Returns 'found' or 'not_found'. Manual ids are never replaced by search."""
    p = tpdb_resolve(c, token, row, call)
    if not p:
        if not row["tpdb_manual"]: c.execute("UPDATE people SET tpdb_performer_id=?, tpdb_fetched_at=? WHERE id=?", (TPDB_NONE, time.time(), row["id"])); c.commit()
        return "not_found"
    apply_tpdb(c, c.execute("SELECT * FROM people WHERE id=?", (row["id"],)).fetchone(), p, allow_image, fill_only)
    return "found"


def replace_credit_names(field, old, new):
    """Replace a comma-separated credit entry that fold-matches `old` with `new`. Returns (value, changed)."""
    if not field: return field, False
    old_k = db.fold(old); new = " ".join((new or "").split())
    if not old_k or not new: return field, False
    out, changed = [], False
    for x in field.split(","):
        n = " ".join(x.split())
        if n and db.fold(n) == old_k:
            out.append(new); changed = True
        elif n:
            out.append(n)
    return (", ".join(out) if out else None), changed

def rewrite_person_credits(c, old_name, new_name):
    """Rewrite film (and excluded) credit lists when a person's primary name changes. Also re-keys cast_roles.
    Returns the number of film rows updated."""
    old_name, new_name = " ".join((old_name or "").split()), " ".join((new_name or "").split())
    if not old_name or not new_name or old_name == new_name: return 0
    cols_have = {r[1] for r in c.execute("PRAGMA table_info(films)")}
    use = [col for col in db.PEOPLE_ROLES.values() if col in cols_have]
    now = time.time(); n = 0
    if use:
        extra = ", cast_roles" if "cast_roles" in cols_have else ""
        q = f"SELECT id{''.join(', '+col for col in use)}{extra} FROM films WHERE " + " OR ".join(
            f"COALESCE({col},'')<>''" for col in use)
        old_k = db.fold(old_name)
        for r in c.execute(q):
            sets = {}
            for col in use:
                nv, ch = replace_credit_names(r[col], old_name, new_name)
                if ch: sets[col] = nv
            if "cast_roles" in cols_have:
                try: roles = json.loads(r["cast_roles"] or "{}") or {}
                except Exception: roles = {}
                if isinstance(roles, dict) and roles:
                    new_roles, rch = {}, False
                    for k, v in roles.items():
                        if db.fold(k or "") == old_k:
                            if new_name not in new_roles: new_roles[new_name] = v
                            rch = True
                        else:
                            new_roles[k] = v
                    if rch: sets["cast_roles"] = json.dumps(new_roles, ensure_ascii=False)
            if sets:
                sets["updated_at"] = now
                c.execute("UPDATE films SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                          list(sets.values()) + [r["id"]])
                n += 1
    xcols = {r[1] for r in c.execute("PRAGMA table_info(excluded)")}
    if "cast_" in xcols:
        for r in c.execute("SELECT id, cast_ FROM excluded WHERE COALESCE(cast_,'')<>''"):
            nv, ch = replace_credit_names(r["cast_"], old_name, new_name)
            if ch: c.execute("UPDATE excluded SET cast_=? WHERE id=?", (nv, r["id"]))
    return n

def _rename_conflict_msg(c, other, new_name, pid):
    if other["merged_into"] == pid:
        return "A merged person already uses that name. Undo the merge first, then make it primary."
    if other["merged_into"]:
        t = c.execute("SELECT name FROM people WHERE id=?", (other["merged_into"],)).fetchone()
        who = t["name"] if t else "someone else"
        return f"A person named '{other['name']}' already exists (merged into {who}). Merge them instead."
    return f"A person named '{other['name']}' already exists. Merge them instead."

def apply_rename(c, row, new_name):
    """Set a person's primary name. The old name becomes an alias; the new name is dropped from aliases.
    Film credits under the old name are rewritten to the new spelling so pages keep linking.
    Returns (payload, http_status). Does not commit. If the new name already belongs to another
    person (as a primary name or alias), refuses and suggests Merge."""
    new_name = " ".join((new_name or "").split())
    if not new_name: return dict(error="Name is required"), 400
    if row["merged_into"]: return dict(error="This person was merged into someone else"), 400
    old_name = row["name"]
    try: old_als = [a for a in json.loads(row["aliases"] or "[]") if isinstance(a, str)]
    except Exception: old_als = []
    if new_name == old_name:
        return dict(id=row["id"], name=old_name, aliases=old_als, credits_updated=0, unchanged=True), 200
    new_k = db.fold(new_name); old_k = row["name_norm"]
    other = c.execute("SELECT * FROM people WHERE name_norm=? AND id<>?", (new_k, row["id"])).fetchone()
    if other: return dict(error=_rename_conflict_msg(c, other, new_name, row["id"])), 400
    for r2 in c.execute("SELECT id, name, aliases FROM people WHERE id<>? AND merged_into IS NULL AND aliases IS NOT NULL", (row["id"],)):
        try: oal = json.loads(r2["aliases"] or "[]")
        except Exception: oal = []
        if any(isinstance(a, str) and db.fold(a) == new_k for a in oal):
            return dict(error=f"'{new_name}' is already an alias of {r2['name']}. Merge them instead."), 400
    als = [a for a in old_als if a.strip() and db.fold(a) != new_k]
    if old_k != new_k and db.fold(old_name) not in {db.fold(a) for a in als}:
        als.append(old_name)
    edited = set(json.loads(row["edited_fields"] or "[]")) | {"name"}
    if als != old_als: edited.add("aliases")
    c.execute("UPDATE people SET name=?, name_norm=?, aliases=?, edited_fields=? WHERE id=?",
              (new_name, new_k, json.dumps(als) if als else None, json.dumps(sorted(edited)), row["id"]))
    n = rewrite_person_credits(c, old_name, new_name)
    try:
        if old_k != new_k:
            if c.execute("SELECT 1 FROM birthdate_lookup WHERE name_norm=?", (new_k,)).fetchone():
                c.execute("DELETE FROM birthdate_lookup WHERE name_norm=?", (old_k,))
            else:
                c.execute("UPDATE birthdate_lookup SET name_norm=?, name=? WHERE name_norm=?", (new_k, new_name, old_k))
        else:
            c.execute("UPDATE birthdate_lookup SET name=? WHERE name_norm=?", (new_name, old_k))
    except Exception:
        pass
    try: c.execute("UPDATE tmdb_match_review SET name=? WHERE person_id=?", (new_name, row["id"]))
    except Exception: pass
    db._AG["key"] = None
    return dict(id=row["id"], name=new_name, aliases=als, credits_updated=n), 200

def init(app, tmdb, posters_dir):
    PHOTO_DIR = os.path.join(posters_dir, "people")
    fold = db.fold

    def C():
        c = db.connect(); c.executescript(PEOPLE_SCHEMA); migrate(c); return c

    # ---------- library people index (fold(name) -> names, roles, film ids) ----------
    IDX = {"at": 0, "key": None, "data": {}, "chk": 0}
    IDX_LOCK = threading.Lock()
    def index(c):
        # inside db.batch_cache() (loops over many people) the films check runs once a minute instead of once per person
        if db._BATCH[0] and IDX["key"] is not None and time.time() - IDX["chk"] < 60: return IDX["data"]
        IDX["chk"] = time.time()
        key = tuple(c.execute("SELECT COUNT(*), MAX(updated_at), SUM(LENGTH(COALESCE(director,'')||COALESCE(cast_,'')||COALESCE(writers,'')||"
                              "COALESCE(producers,'')||COALESCE(cinematographer,'')||COALESCE(composer,''))) FROM films").fetchone())
        with IDX_LOCK:
            if IDX["key"] == key: return IDX["data"]
            data = {}
            cols = ", ".join(ROLE_COLS.values())
            for r in c.execute(f"SELECT id, {cols} FROM films"):
                for role, col in ROLE_COLS.items():
                    for nm in (r[col] or "").split(","):
                        nm = " ".join(nm.split())
                        if not nm: continue
                        k = fold(nm)
                        e = data.setdefault(k, {"names": {}, "roles": {}, "films": set()})
                        e["names"][nm] = e["names"].get(nm, 0) + 1
                        e["roles"].setdefault(role, set()).add(r["id"]); e["films"].add(r["id"])
            IDX.update(key=key, data=data, at=time.time())
            return data

    def person_entry(c, row):
        """Library index entry for a person, merged over their name and multi-word aliases (fold-normalised)."""
        idx = index(c); keys = [row["name_norm"]]
        try: als = json.loads(row["aliases"] or "[]")
        except Exception: als = []
        keys += [fold(" ".join(a.split())) for a in als if isinstance(a, str) and len(a.split()) >= 2]
        keys += [fold(n) for n in db.expand_person(c, row["name"])]  # linked aliases and merged people
        out = {"names": {}, "roles": {}, "films": set()}
        for k in dict.fromkeys(keys):
            x = idx.get(k)
            if not x: continue
            for n, v in x["names"].items(): out["names"][n] = out["names"].get(n, 0) + v
            for ro, ids in x["roles"].items(): out["roles"].setdefault(ro, set()).update(ids)
            out["films"] |= x["films"]
        return out

    SCN = {}  # pid -> background ThePornDB scene-list load progress
    SCN_LOCK = threading.Lock()
    def start_scene_load(pid, tok, perf_id):
        """Fetch every page of /performers/{id}/scenes in a background thread (rate-limited by call_retry); the list and the
        API's total are cached in people.tpdb_scenes_json / tpdb_scenes_total. A partial list is saved as pages arrive."""
        with SCN_LOCK:
            if (SCN.get(pid) or {}).get("running"): return
            SCN[pid] = dict(running=True, done=0, total=None, page=0, pages=None, error=None)
        def run():
            import tpdb as tpdbmod
            st = SCN[pid]; c2 = C()
            try:
                allsc, page, seen = [], 1, set()
                while page <= TPDB_MAX_PAGES:
                    r = tpdbmod.call_retry(f"/performers/{perf_id}/scenes", tok, per_page=100, page=page)
                    d = r.get("data") or []
                    new = [x for x in d if str(x.get("id")) not in seen]; seen |= {str(x.get("id")) for x in d}; allsc += new
                    meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
                    last, tot = meta.get("last_page"), meta.get("total")
                    st.update(done=len(allsc), page=page, pages=int(last) if last else None, total=int(tot) if tot else None)
                    final = not d or not new or (last and page >= int(last)) or (not last and len(d) < 100) or page >= TPDB_MAX_PAGES
                    if final or page % 5 == 0:
                        keep = [dict(id=str(x.get("id")), _id=str(x.get("_id") or ""), title=x.get("title"), date=(x.get("date") or "")[:10],
                                     studio=(x.get("site") or {}).get("name"), label=site_label(x.get("site")), slug=x.get("slug"),
                                     thumb=((x.get("posters") or {}).get("small") if isinstance(x.get("posters"), dict) else None) or x.get("poster") or x.get("image"))
                                for x in allsc]
                        c2.execute("UPDATE people SET tpdb_scenes_json=?, tpdb_scenes_at=?, tpdb_scenes_total=? WHERE id=?",
                                   (json.dumps(keep), time.time() if final else 0, st["total"] or len(keep), pid)); c2.commit()
                    if final: break
                    page += 1
            except Exception as ex: st["error"] = type(ex).__name__
            finally:
                st["running"] = False; c2.close()
                try: scenes_changed(pid)
                except Exception: pass
        threading.Thread(target=run, daemon=True).start()
    def scenes_changed(pid):
        """A scene list finished loading: drop every cache built from the partial list (insights, credits, built list)."""
        for cache in (INS, CO_ITEMS, SALL):
            for k in [k for k in list(cache) if k[0] == pid]: cache.pop(k, None)
        BUILT.clear()

    def best_name(e): return max(e["names"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    def top_role(e): return max(e["roles"].items(), key=lambda kv: len(kv[1]))[0]
    def photo_url(row):
        has = row and (row["photo_file"] or photo_choice(row))
        return f"/api/person/{row['id']}/photo?v={int(row['fetched_at'] or 0)}" if has else None

    def follow_merge(c, r):
        hops = 0
        while r and r["merged_into"] and hops < 5:
            t = c.execute("SELECT * FROM people WHERE id=?", (r["merged_into"],)).fetchone()
            if not t: break
            r = t; hops += 1
        return r

    def ensure_row(c, name):
        name = " ".join((name or "").split()); k = fold(name)
        if not k: return None
        r = c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()
        if r: return follow_merge(c, r)
        # exact alias (including a single-word former primary) of an existing person
        for row in c.execute("SELECT * FROM people WHERE aliases IS NOT NULL"):
            try: als = json.loads(row["aliases"] or "[]")
            except Exception: als = []
            if any(isinstance(a, str) and fold(" ".join(a.split())) == k for a in als):
                return follow_merge(c, row)
        c.execute("INSERT INTO people(name, name_norm) VALUES(?,?)", (name, k)); c.commit()
        return c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()

    def name_order(hits, q):
        """One search word: decide first vs last name by how many hits have it as their first vs last word (tie = first);
        those hits come first (first name: A-Z by full name; last name: A-Z by first name), then everyone else A-Z.
        Several words or no first/last match: A-Z by name. Keys are already accent/case folded."""
        az = lambda ke: ke[0]
        if len(q) != 1: return sorted(hits, key=az)
        w = q[0]
        nf = sum(1 for k, _ in hits if k.split()[:1] == [w])
        nl = sum(1 for k, _ in hits if len(k.split()) > 1 and k.split()[-1] == w)
        if not nf and not nl: return sorted(hits, key=az)
        if nf >= nl: top = lambda k: k.split()[0] == w; key = az
        else: top = lambda k: len(k.split()) > 1 and k.split()[-1] == w; key = lambda ke: (ke[0].split()[0], ke[0])
        return sorted((h for h in hits if top(h[0])), key=key) + sorted((h for h in hits if not top(h[0])), key=az)

    @app.get("/api/people/search")
    def people_search():
        """Accent/case-insensitive name search over everyone credited in the library."""
        q = fold(request.args.get("q") or "").split()
        eth = " ".join((request.args.get("eth") or "").split())
        if not q and not eth: return jsonify(people=[], total=0)
        lim = min(int(request.args.get("limit", 24)), 1000 if eth and not q else 200)  # Ethnicity filter alone lists everyone with it
        c = C(); data = index(c)
        lib = (request.args.get("lib") or "").strip().rstrip("\\/")
        scoped = bool(lib); left_out = 0
        if scoped:  # only credits on titles in the chosen library folder (same folder match as /api/films?lib=)
            ctx = {}; data = lib_scope(c, data, lib, ctx)
            left_out = sum(1 for k in data.pop(None, ()) if all(w in k for w in q))
        attr = {r["name_norm"]: r for r in c.execute("SELECT name_norm, ethnicity, nationality FROM people WHERE merged_into IS NULL AND "
                                                       "(COALESCE(ethnicity,'')<>'' OR COALESCE(nationality,'')<>'')")}
        if eth:  # Ethnicity filter: exact stored value
            data = {k: e for k, e in data.items() if k in attr and eth in eth_parts(attr[k]["ethnicity"])}
            if scoped: left_out = 0
        hits = [(k, e) for k, e in data.items() if all(w in k for w in q)]
        if q:  # also match ethnicity / nationality: a known ethnicity word matches that value exactly, else word prefixes
            qe = norm_ethnicity(" ".join(q), keep=False)
            have0 = {k for k, _ in hits}
            for k, r in attr.items():
                if k in have0 or k not in data: continue
                if qe: ok = qe in [norm_ethnicity(x) for x in eth_parts(r["ethnicity"])]
                else:
                    toks = re.split(r"[^a-z0-9]+", fold(f"{r['ethnicity'] or ''} {r['nationality'] or ''}"))
                    ok = all(any(t.startswith(w) for t in toks if t) for w in q)
                if ok: hits.append((k, data[k]))
        def score(ke):
            k, e = ke; toks = k.split()
            return (0 if k == " ".join(q) else 1 if all(any(t.startswith(w) for t in toks) for w in q) else 2, -len(e["films"]), k)
        hits.sort(key=score)
        have = {k for k, _ in hits}
        for r in ([] if scoped else c.execute("SELECT name, name_norm FROM people WHERE source='manual'")):  # no credits, so never in a folder
            if not eth and r["name_norm"] not in have and all(w in r["name_norm"] for w in q):
                hits.append((r["name_norm"], {"names": {r["name"]: 1}, "roles": {}, "films": set()}))
        for k2, names in (db.alias_groups(c).items() if q else ()):  # a search for any alias finds the person
            if all(w in k2 for w in q):
                for n in names:
                    kn = fold(n)
                    if kn not in have and kn in data: hits.append((kn, data[kn])); have.add(kn)
        hits = name_order(hits, q)
        adult_ids = adult_film_ids(c)
        rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM people WHERE name_norm IN (%s)" % ",".join("?" * len(hits[:lim])), [k for k, _ in hits[:lim]])} if hits else {}
        out = []; seen_pid = set()
        for k, e in hits[:lim]:
            r = rows.get(k); role = top_role(e) if e["roles"] else "cast"
            if r and r["merged_into"]:
                t = c.execute("SELECT * FROM people WHERE id=?", (r["merged_into"],)).fetchone()
                if t:
                    r = t; k = t["name_norm"]; e = dict(person_entry(c, t), names={t["name"]: 1})
                    if scoped:
                        e = lib_scope(c, {k: e}, lib, ctx).get(k)
                        if not e: continue
            if r and r["id"] in seen_pid: continue
            if r: seen_pid.add(r["id"])
            out.append(dict(name=best_name(e), key=k, pid=r["id"] if r else None, n=len(e["films"]), role=role,
                            known_for=(r["known_for"] if r and r["known_for"] else ROLE_DEPT[role]),
                            roles={ro: len(s) for ro, s in e["roles"].items()}, photo=photo_url(r), fav=bool(r and r["favorite"]),
                            adult=bool(e["films"] & adult_ids)))
        return jsonify(people=out, total=len(hits), lib=lib or None, age_left_out=left_out if scoped else None)

    def lib_scope(c, data, lib, ctx=None):
        """People index entries cut down to credits on titles under library folder `lib` (db.lib_prefixes: all scene
        roots count as one library). People with no credit there are dropped. Age rule: on adult titles a performer
        (cast credit) counts only with a known birth date and 18+ at release; key None = the keys of people dropped
        only because of that rule."""
        ctx = {} if ctx is None else ctx  # folder ids / birth dates, reused within one request
        if "ids" not in ctx: ctx.update(lib_scope_ctx(c, lib))
        ids, adult, fd, prow, byid, ag = (ctx[x] for x in ("ids", "adult", "fd", "prow", "byid", "ag"))
        def bday(k):
            pr = prow.get(k) or next((prow[fold(x)] for x in ag.get(k) or [] if fold(x) in prow), None); hops = 0
            while pr and pr["merged_into"] and hops < 5:
                nx = byid.get(pr["merged_into"]); hops += 1
                if not nx: break
                pr = nx
            return _bd(pr["birthday"]) if pr else None
        out, dropped = {}, set()
        for k, e in data.items():
            if k is None or not (e["films"] & ids): continue
            roles = {ro: s & ids for ro, s in e["roles"].items()}
            hidden = set()
            ac = roles.get("cast", set()) & adult
            if ac:
                bd = bday(k)
                hidden = {i for i in ac if not (bd and _adult_ok(bd, *fd.get(i, (None, None))))}
                if hidden: roles["cast"] = roles["cast"] - hidden
            roles = {ro: s for ro, s in roles.items() if s}
            films = set().union(*roles.values()) if roles else set()
            if not films:
                if hidden: dropped.add(k)
                continue
            out[k] = {"names": e["names"], "roles": roles, "films": films}
        out[None] = dropped
        return out

    def lib_scope_ctx(c, lib):
        likes, args = [], []
        for pre in db.lib_prefixes(c, lib):
            e_ = pre.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            likes += ["LOWER(path) LIKE ? ESCAPE '\\'"] * 2; args += [e_ + "\\\\%", e_ + "/%"]
        ids = {r[0] for r in c.execute("SELECT DISTINCT film_id FROM files WHERE " + " OR ".join(likes), args)} if likes else set()
        adult = adult_film_ids(c) & ids
        fd = {}
        if adult:
            for r in c.execute(f"SELECT id, release_date, year FROM films WHERE id IN ({','.join(map(str, adult))})"): fd[r[0]] = (r[1], r[2])
        prow = {r["name_norm"]: r for r in c.execute("SELECT id, name_norm, birthday, merged_into FROM people")} if adult else {}
        byid = {r["id"]: r for r in prow.values()}
        return dict(ids=ids, adult=adult, fd=fd, prow=prow, byid=byid, ag=db.alias_groups(c) if adult else {})

    @app.get("/api/person/lookup")
    def person_lookup():
        name = (request.args.get("name") or "").strip()
        if not name: return jsonify(error="name required"), 400
        c = C(); r = ensure_row(c, name); return jsonify(id=r["id"], name=r["name"])

    # ---------- TMDB ----------
    def key_of(c): return db.get_setting(c, "tmdb_key", "") or ""

    def resolve(c, key, row, lib_ids):
        """Find the TMDB person id: first via the library films' TMDB credits, else TMDB person search."""
        target = row["name_norm"]
        tids = [tuple(r) for r in c.execute(f"SELECT tmdb_id, tmdb_kind FROM films WHERE tmdb_id IS NOT NULL AND id IN ({','.join('?'*len(lib_ids))}) ORDER BY year DESC LIMIT 4", list(lib_ids))] if lib_ids else []
        for tid, kind in tids:
            try: cr = load_title_credits(tmdb, key, tid, kind)
            except urllib.error.HTTPError as e:
                if e.code == 401: raise
                continue
            for p in cr.get("cast", []) + cr.get("crew", []):
                if fold(p.get("name") or "") == target or fold(p.get("original_name") or "") == target: return p["id"]
        res = tmdb("search/person", key, query=row["name"], include_adult="true").get("results", [])
        exact = [p for p in res if fold(p.get("name") or "") == target or fold(p.get("original_name") or "") == target]
        ad = adult_film_ids(c)
        if lib_ids and set(lib_ids) <= ad and not set(lib_ids) & classic_film_ids(c):
            # scene performers: a same-named mainstream TMDB person (e.g. a casting director) is not them; only an
            # adult-flagged TMDB person, or one whose known-for titles are adult, counts as a match
            exact = [p for p in exact if p.get("adult") or any(k.get("adult") for k in p.get("known_for") or [])]
        if not exact: return NO_MATCH
        owned = {r[0] for r in c.execute("SELECT tmdb_id FROM films WHERE tmdb_id IS NOT NULL")}
        exact.sort(key=lambda p: (-sum(1 for k in p.get("known_for", []) if k.get("id") in owned), -(p.get("popularity") or 0)))
        return exact[0]["id"]

    def classic_resolve(c, key, row, lib_ids):
        """TMDB person id for someone in Classic Porn, only when confident: (1) their name or an alias is credited on one of
        their Classic Porn films' TMDB credits; else (2) TMDB person search by name + aliases, taking the single exact-name
        hit, or the single one whose known_for overlaps their Classic Porn / library films. Several candidates -> None and
        the person is listed in tmdb_match_review; nothing -> NO_MATCH."""
        c.execute(REVIEW_SCHEMA)
        try: als = [a for a in json.loads(row["aliases"] or "[]") if isinstance(a, str) and db._alias_ok(a)]
        except Exception: als = []
        names = list(dict.fromkeys([row["name"]] + [n for n in db.expand_person(c, row["name"])[1:] if db._alias_ok(n)] + als))
        keys = {fold(n) for n in names}
        ids = list(lib_ids or [])
        cl = classic_film_ids(c) & set(ids)
        q = lambda s_: [tuple(r) for r in c.execute(f"SELECT tmdb_id, tmdb_kind FROM films WHERE tmdb_id IS NOT NULL AND id IN ({','.join('?'*len(s_))}) ORDER BY year DESC", list(s_))] if s_ else []
        ctids = q(cl); ltids = {tid for tid, _k in q(set(ids))}
        hit = {}
        for tid, kind in ctids[:6]:
            try: cr = load_title_credits(tmdb, key, tid, kind)
            except urllib.error.HTTPError as e:
                if e.code == 401: raise
                continue
            for p in cr.get("cast", []) + cr.get("crew", []):
                if fold(p.get("name") or "") in keys or fold(p.get("original_name") or "") in keys: hit.setdefault(p["id"], p.get("name"))
            if len(hit) == 1: break
        if len(hit) == 1:
            c.execute("DELETE FROM tmdb_match_review WHERE person_id=?", (row["id"],)); return next(iter(hit))
        cands = {}
        for n in names[:5]:
            for p in tmdb("search/person", key, query=n, include_adult="true").get("results", []):
                if fold(p.get("name") or "") in keys or fold(p.get("original_name") or "") in keys: cands.setdefault(p["id"], p)
        if hit: cands = {k: v for k, v in cands.items() if k in hit} or {k: {"id": k, "name": v} for k, v in hit.items()}
        if not cands:
            c.execute("DELETE FROM tmdb_match_review WHERE person_id=?", (row["id"],)); return NO_MATCH
        if len(cands) > 1:
            ov = [p for p in cands.values() if any(k.get("id") in ltids for k in p.get("known_for") or [])]
            if len(ov) == 1: cands = {ov[0]["id"]: ov[0]}
        if len(cands) == 1:
            c.execute("DELETE FROM tmdb_match_review WHERE person_id=?", (row["id"],)); return next(iter(cands))
        c.execute("INSERT OR REPLACE INTO tmdb_match_review VALUES(?,?,?,?,?)",
                  (row["id"], row["name"], json.dumps([dict(id=p["id"], name=p.get("name"), known_for=[k.get("title") or k.get("name") for k in p.get("known_for") or []][:3])
                                                     for p in cands.values()]), "several TMDB people match", time.time()))
        c.commit(); return None

    def fetch(c, key, row, keep_profile=False, tmdb_primary=False):
        p = tmdb(f"person/{row['tmdb_person_id']}", key, append_to_response="combined_credits", include_adult="true")
        cc = p.get("combined_credits") or {}
        keep = lambda x: (x.get("media_type") or "movie") in ("movie", "tv")
        credits = dict(cast=[_norm_combined_credit(x, "cast") for x in cc.get("cast", []) if keep(x)],
                       crew=[_norm_combined_credit(x, "crew") for x in cc.get("crew", []) if keep(x)])
        edited = set(json.loads(row["edited_fields"] or "[]"))
        sets = dict(profile_path=p.get("profile_path"), bio=p.get("biography"), birthday=p.get("birthday"), deathday=p.get("deathday"),
                    place=p.get("place_of_birth"), known_for=p.get("known_for_department"), credits_json=json.dumps(credits),
                    fetched_at=time.time(), source="tmdb")
        if keep_profile:  # bio/photo/etc. come from ThePornDB or hand edits: only take the filmography (and a birth date ThePornDB lacks)
            sets = dict(credits_json=sets["credits_json"], known_for=sets["known_for"], fetched_at=sets["fetched_at"],
                        **({"birthday": sets["birthday"]} if sets["birthday"] and not row["birthday"] else {}),
                        **({"deathday": sets["deathday"]} if sets["deathday"] else {}))
        if tmdb_primary:  # TMDB is the primary profile; fields TMDB lacks keep what ThePornDB / others filled in
            sets = {k: v for k, v in sets.items() if v not in (None, "")}; sets["source"] = "tmdb"
        if not sets.get("deathday") or row["deathday"]: sets.pop("deathday", None)  # death date is add-only
        if not sets.get("birthday"): sets.pop("birthday", None)  # TMDB has none: keep one found elsewhere
        elif sets["birthday"] != row["birthday"]: sets["birth_date_source"] = f"tmdb person {row['tmdb_person_id']}"
        sets = {k: v for k, v in sets.items() if k not in edited and not (k == "birth_date_source" and "birthday" in edited)
                and not (k in PHOTO_AUTO and photo_locked(row))}  # a hand-set photo is never replaced by a refresh
        g = TMDB_GENDER.get(p.get("gender"))
        if g and not row["gender"] and "gender" not in edited: sets["gender"] = g  # add-only
        c.execute("UPDATE people SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", list(sets.values()) + [row["id"]])
        c.commit()
        if not row["deathday"] and "deathday" not in sets and "deathday" not in edited and "pytest" not in __import__("sys").modules:
            threading.Thread(target=death_wikidata, args=([row["id"]],), daemon=True).start()  # Wikidata P570 (TMDB id link), off the request
        if not row["ethnicity"] and "ethnicity" not in edited and not row["ethnicity_checked_at"] and "pytest" not in __import__("sys").modules:
            threading.Thread(target=eth_wikidata, args=([row["id"]],), daemon=True).start()  # Wikidata P172, only when published

    def filmography(c, credits):
        lib = [dict(r) for r in c.execute("SELECT id, title, original_title, year, tmdb_id, tmdb_kind, poster, custom_poster FROM films")]
        by_tmdb = {}
        by_tmdb_id = {}
        for f in lib:
            if f["tmdb_id"]:
                k = tmdb_title_kind(f.get("tmdb_kind"))
                by_tmdb[(f["tmdb_id"], k)] = f
                by_tmdb_id.setdefault(f["tmdb_id"], f)
        by_ty = {}
        for f in lib:
            for t in (f["title"], f["original_title"]):
                if t and f["year"]: by_ty.setdefault((fold(t), f["year"]), f)
        groups = {}
        def add(dept, x, what):
            yr = int(x["release_date"][:4]) if (x.get("release_date") or "")[:4].isdigit() else None
            g = groups.setdefault(dept, {})
            kind = tmdb_title_kind(item=x)
            e = g.get((x["id"], kind))
            if e:
                if what and what not in e["role"].split(" / "): e["role"] += " / " + what
                return
            own = by_tmdb.get((x["id"], kind))
            if own is None and kind == "movie":
                own = by_tmdb_id.get(x["id"])
            if own is None:
                own = yr and (by_ty.get((fold(x.get("title") or ""), yr)) or by_ty.get((fold(x.get("original_title") or ""), yr)))
            g[(x["id"], kind)] = dict(tmdb_id=x["id"], title=x.get("title") or x.get("original_title"), year=yr, date=x.get("release_date") or "",
                              role=what or "", thumb=x.get("poster_path") and "https://image.tmdb.org/t/p/w92" + x["poster_path"],
                              film_id=own["id"] if own else None,
                              poster=(own["custom_poster"] or own["poster"]) if own else None,
                              media_type=kind, tmdb_kind=kind, url=tmdb_page_url(x["id"], kind))
        for x in credits.get("cast", []): add("Acting", x, (x.get("character") or "").strip())
        for x in credits.get("crew", []): add(x.get("department") or "Crew", x, x.get("job"))
        depts = sorted(groups, key=lambda d: (DEPT_ORDER.index(d) if d in DEPT_ORDER else 99, d))
        return [dict(dept=d, credits=sorted(groups[d].values(), key=lambda e: (e["year"] is None, e["date"]))) for d in depts]

    @app.get("/api/person/<int:pid>")
    def person_page(pid):
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        hops = 0
        while row["merged_into"] and hops < 5:  # merged into someone else: show that person
            t = c.execute("SELECT * FROM people WHERE id=?", (row["merged_into"],)).fetchone()
            if not t: break
            row = t; pid = t["id"]; hops += 1
        e = person_entry(c, row)
        lib_ids = sorted(e["films"])
        status, msg = "ok", None
        adult = bool(set(lib_ids) & adult_film_ids(c))
        mainstream = bool(set(lib_ids) - adult_film_ids(c))
        classic = bool(set(lib_ids) & classic_film_ids(c))  # anyone in Classic Porn: TMDB primary, ThePornDB fills gaps
        tp_note = None
        if adult:
            tok = db.get_setting(c, "tpdb_key", "") or ""
            if not tok: tp_note = "No ThePornDB token set."
            else:
                stale = time.time() - (row["tpdb_fetched_at"] or 0) > STALE_S
                if row["tpdb_performer_id"] is None or request.args.get("refresh") or (stale and row["tpdb_performer_id"] != TPDB_NONE):
                    import tpdb as tpdbmod
                    try: tpdb_update(c, tok, row, lambda path, **kw: tpdbmod.call_retry(path, tok, **kw), allow_image=not (set(lib_ids) & review_film_ids(c)),
                                     fill_only=classic and row["source"] != "manual")
                    except Exception as ex: tp_note = f"Could not reach ThePornDB ({type(ex).__name__})."
                    row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        tpdb_linked = adult and row["tpdb_performer_id"] not in (None, TPDB_NONE)
        on_tpdb = tpdb_linked and row["source"] == "tpdb" and not classic  # ThePornDB-primary profile (scene performers)
        key = key_of(c)
        manual = row["source"] == "manual" and not on_tpdb
        tm_err = None
        if key and not (row["tmdb_manual"] and row["tmdb_person_id"] == NO_MATCH):
            try:  # TMDB filmography for everyone with a TMDB person (profile fields stay from TPDB/hand edits)
                if row["tmdb_person_id"] is None or (request.args.get("refresh") and not row["tmdb_manual"] and row["tmdb_person_id"] == NO_MATCH):
                    if classic and not manual:
                        held = c.execute("SELECT 1 FROM tmdb_match_review WHERE person_id=?", (pid,)).fetchone()
                        tid = None if held and not request.args.get("refresh") else classic_resolve(c, key, row, lib_ids)
                    else: tid = resolve(c, key, row, lib_ids)
                    if tid is not None:
                        c.execute("UPDATE people SET tmdb_person_id=? WHERE id=?", (tid, pid)); c.commit()
                        row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
                tmdb_primary = classic and not manual
                if row["tmdb_person_id"] and row["tmdb_person_id"] > 0 and (not row["credits_json"] or request.args.get("refresh")
                                                                              or time.time() - (row["fetched_at"] or 0) > STALE_S
                                                                              or (tmdb_primary and row["source"] == "tpdb" and "source" not in json.loads(row["edited_fields"] or "[]"))):
                    fetch(c, key, row, keep_profile=on_tpdb or manual, tmdb_primary=tmdb_primary); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
            except urllib.error.HTTPError as ex:
                tm_err = ("TMDB rejected the API key (401)." if ex.code == 401 else f"TMDB error {ex.code}.")
            except __import__("sqlite3").OperationalError as ex:
                tm_err = f"The library database was busy ({ex}); showing cached/library data. Try again in a moment."
            except Exception as ex:
                tm_err = f"Could not reach TMDB ({type(ex).__name__}); showing cached/library data."
        if not row["birthday"] and "birthday" not in json.loads(row["edited_fields"] or "[]"):
            try:
                import birthdates; birthdates.lookup_async(C, row["name"])  # Wikidata / Wikipedia, saved only on a confident match
            except Exception: pass
        on_tmdb = (row["tmdb_person_id"] or 0) > 0
        if on_tpdb: status, msg = "ok", tp_note
        elif manual: status, msg = "ok", None
        elif tm_err: status, msg = "offline", tm_err
        elif on_tmdb: status, msg = "ok", None
        elif adult and not mainstream: status, msg = "no_match", tp_note or "Not found on ThePornDB or TMDB. You can set either id manually."
        elif not key: status, msg = "no_key", "No TMDB API key set, so only the films in your library are shown."
        else: status, msg = "no_match", "No matching person found on TMDB. You can set the TMDB person id manually."
        scenes = None; scenes_loading = None; extra_owned = 0; tp_count = 0
        if tpdb_linked:
            tok = db.get_setting(c, "tpdb_key", "") or ""
            if tok and (not row["tpdb_scenes_json"] or request.args.get("refresh") or time.time() - (row["tpdb_scenes_at"] or 0) > STALE_S):
                start_scene_load(pid, tok, row["tpdb_performer_id"])
            load = SCN.get(pid)
            if load and load.get("running"): scenes_loading = dict(done=load["done"], total=load["total"], pages=load["pages"], page=load["page"])
            elif load and load.get("error"): msg = (msg + " " if msg else "") + f"Could not load all ThePornDB scenes ({load['error']}); showing cached list."
            if row["tpdb_scenes_json"]:
                scenes = [dict(title=x["title"], date=x["date"], studio=x["studio"], url=f"https://theporndb.net/scenes/{x.get('slug') or x['id']}",
                               film_id=None, thumb=x["thumb"], poster=None, _ids=(str(x["id"]), str(x.get("_id") or "-"))) for x in json.loads(row["tpdb_scenes_json"])]
        if adult:  # ThePornDB scene lists cached on alias / merged people rows (another performer id for the same person)
            others = [fold(n) for n in db.expand_person(c, row["name"])][1:]
            if others:
                seen_tp = {i for z in (scenes or []) for i in z["_ids"]}
                for r2 in c.execute(f"SELECT tpdb_scenes_json FROM people WHERE id<>? AND tpdb_scenes_json IS NOT NULL AND name_norm IN ({','.join('?'*len(others))})", [pid] + others):
                    for x in json.loads(r2[0] or "[]"):
                        ids = (str(x["id"]), str(x.get("_id") or "-"))
                        if ids[0] in seen_tp: continue
                        seen_tp.add(ids[0]); scenes = scenes or []
                        scenes.append(dict(title=x["title"], date=x["date"], studio=x["studio"], url=f"https://theporndb.net/scenes/{x.get('slug') or x['id']}",
                                           film_id=None, thumb=x["thumb"], poster=None, _ids=ids))
        if adult:  # ALL of the user's library scenes for this person (name/aliases or ThePornDB scene id), listed first
            img = not (set(lib_ids) & review_film_ids(c))
            tp_ids = sorted({i for z in (scenes or []) for i in z["_ids"] if i and i != "-"})
            q = "SELECT id, title, release_date, year, studio, tpdb_id, poster, custom_poster FROM films WHERE kind='scene' AND (0"
            args = []
            for chunk in [lib_ids[i:i + 500] for i in range(0, len(lib_ids), 500)]: q += f" OR id IN ({','.join('?'*len(chunk))})"; args += chunk
            for chunk in [tp_ids[i:i + 500] for i in range(0, len(tp_ids), 500)]: q += f" OR tpdb_id IN ({','.join('?'*len(chunk))})"; args += chunk
            mine = {r["id"]: r for r in c.execute(q + ")", args)}
            have_tp = {str(r["tpdb_id"]) for r in mine.values() if r["tpdb_id"]}
            tp_count = len(scenes or []); tps = set(tp_ids)
            extra_owned = sum(1 for r in mine.values() if not r["tpdb_id"] or str(r["tpdb_id"]) not in tps)  # owned but not in the ThePornDB list
            tp_thumb = {i: z["thumb"] for z in (scenes or []) for i in z["_ids"] if i and i != "-"}
            owned_list = [dict(title=r["title"], date=r["release_date"] or (str(r["year"]) if r["year"] else None), studio=r["studio"], url=None,
                               film_id=r["id"], thumb=tp_thumb.get(str(r["tpdb_id"] or "-")) if img else None,
                               poster=((r["custom_poster"] or r["poster"]) if img else None)) for r in mine.values()]
            rest = []
            for z in (scenes or []):
                if any(i in have_tp for i in z.pop("_ids")): continue  # already listed from the library
                if not img: z["thumb"] = None
                rest.append(z)
            owned_list.sort(key=lambda z: z["date"] or ""); rest.sort(key=lambda z: z["date"] or "")
            if owned_list or scenes is not None: scenes = owned_list + rest
        elif scenes:
            for z in scenes: z.pop("_ids", None)
        lib = [dict(r) for r in c.execute(f"SELECT id, title, year, poster, custom_poster, tmdb_id FROM films WHERE id IN ({','.join('?'*len(lib_ids))}) ORDER BY year IS NULL, year, title COLLATE FOLD", lib_ids)] if lib_ids else []
        for f in lib: f["roles"] = [ro for ro, s in e["roles"].items() if f["id"] in s]
        groups = filmography(c, json.loads(row["credits_json"])) if row["credits_json"] and (row["tmdb_person_id"] or 0) > 0 else None
        total = owned = 0
        role_ids = {ro: set(s) for ro, s in e["roles"].items()}
        if groups:
            allm = {}
            for g in groups:
                for x in g["credits"]:
                    allm[x["tmdb_id"]] = x["film_id"]
                    if not x["film_id"]: continue
                    # library titles whose TMDB credit names them but whose own top-billed cast/crew list doesn't still count in the header
                    if g["dept"] == "Acting": role_ids.setdefault("cast", set()).add(x["film_id"])
                    elif g["dept"] == "Directing" and "Director" in (x["role"] or "").split(" / "): role_ids.setdefault("director", set()).add(x["film_id"])
            total, owned = len(allm), sum(1 for v in allm.values() if v)
        bd_lookup = None
        if not _bd(row["birthday"]):
            try:
                r3 = c.execute("SELECT status, detail, at FROM birthdate_lookup WHERE name_norm=?", (row["name_norm"],)).fetchone()
                if r3: bd_lookup = dict(status=r3[0], detail=r3[1], at=r3[2])
            except Exception: pass
        tp = row["tmdb_person_id"] if (row["tmdb_person_id"] or 0) > 0 else None
        names = list(e["names"]) or [row["name"]]
        tpid = row["tpdb_performer_id"] if row["tpdb_performer_id"] not in (None, TPDB_NONE) else None
        links = json.loads(row["links_json"] or "{}")
        if classic and not manual:  # Classic Porn: TMDB first, ThePornDB only for what TMDB lacks
            source = (("TMDB" + (" + ThePornDB for details TMDB lacks" if tpid else "")) if tp and row["credits_json"] else
                      ("ThePornDB (no TMDB person found yet)" if tpid else None))
        else:
            source = ("ThePornDB" + (" + TMDB filmography" if groups else "")) if on_tpdb else ("Manual (added by hand)" + (" + TMDB filmography" if groups else "")) if manual else ("TMDB" if tp and row["credits_json"] else None)
        return jsonify(status=status, message=msg, person=dict(
            id=pid, name=row["name"], aka=[n for n in names if n != row["name"]], tmdb_person_id=tp, manual=bool(row["tmdb_manual"]),
            source=source, classic=classic, adult=adult, tpdb_performer_id=tpid, tpdb_manual=bool(row["tpdb_manual"]),
            tpdb_url=tpid and f"https://theporndb.net/performers/{tpid}", aliases=json.loads(row["aliases"] or "[]"),
            nationality=row["nationality"], ethnicity=row["ethnicity"], ethnicity_source=row["ethnicity_source"], gender=row["gender"], measurements=row["measurements"], height=row["height"], height_display=fmt_height(row["height"], height_units(c)), career=row["career"],
            links=links, iafd=next((v for k, v in links.items() if k.lower() == "iafd"), None),
            edited=json.loads(row["edited_fields"] or "[]"), photo_cropped=bool(row["photo_orig"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_orig"]))),
            merged=[dict(id=r2["id"], name=r2["name"]) for r2 in c.execute("SELECT id, name FROM people WHERE merged_into=? ORDER BY name", (pid,))],
            gallery_dir=row["gallery_dir"],
            tmdb_url=tp and f"https://www.themoviedb.org/person/{tp}", photo=photo_url(row), bio=row["bio"], birthday=row["birthday"], birth_date_source=row["birth_date_source"],
            deathday=row["deathday"], place=row["place"], fetched_at=row["fetched_at"], favorite=bool(row["favorite"] or c.execute("SELECT 1 FROM people WHERE merged_into=? AND favorite=1", (pid,)).fetchone()),
            known_for=row["known_for"] or (ROLE_DEPT[top_role(e)] if e["roles"] else None)),
            birthday_lookup=bd_lookup, roles={ro: len(s) for ro, s in role_ids.items() if s}, library=lib, groups=groups, total=total, owned=owned, scenes=scenes,
            scenes_owned=sum(1 for z in (scenes or []) if z.get("film_id")),
            scenes_total=(max(row["tpdb_scenes_total"] or 0, tp_count) + extra_owned) if scenes is not None else None,
            scenes_tpdb_total=max(row["tpdb_scenes_total"] or 0, tp_count) or None,
            scenes_loading=scenes_loading)

    @app.post("/api/person/<int:pid>/tmdb")
    def person_set_tmdb(pid):
        """Manual override: {tmdb: 12345 | 'https://www.themoviedb.org/person/12345-name' | '' (re-detect)}."""
        c = C(); v = str((request.json or {}).get("tmdb") or "").strip()
        if not c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone(): return jsonify(error="Unknown person"), 404
        if not v:
            c.execute("UPDATE people SET tmdb_person_id=NULL, tmdb_manual=0, credits_json=NULL, fetched_at=NULL WHERE id=?", (pid,))
        else:
            m = re.fullmatch(r"\d+", v) or re.search(r"/person/(\d+)", v)
            if not m: return jsonify(error=f"Not a TMDB person id or URL: {v}"), 400
            tid = int(m.group(1) if m.groups() else m.group(0))
            c.execute("UPDATE people SET tmdb_person_id=?, tmdb_manual=1, credits_json=NULL, fetched_at=NULL, profile_path=NULL WHERE id=?", (tid, pid))
        c.commit(); return jsonify(ok=True)

    @app.post("/api/person/<int:pid>/tpdb")
    def person_set_tpdb(pid):
        """Manual ThePornDB performer: {tpdb: id | slug | performer URL | '' (auto)}. Loads the profile immediately."""
        c = C(); v = str((request.json or {}).get("tpdb") or "").strip()
        row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        if not v:
            c.execute("UPDATE people SET tpdb_performer_id=NULL, tpdb_manual=0, tpdb_fetched_at=NULL, tpdb_scenes_json=NULL, tpdb_scenes_at=NULL, tpdb_scenes_total=NULL WHERE id=?", (pid,)); c.commit(); return jsonify(ok=True)
        ident = parse_tpdb_performer(v)
        if not ident: return jsonify(error=f"Not a ThePornDB performer id or URL: {v}"), 400
        tok = db.get_setting(c, "tpdb_key", "") or ""
        if not tok: return jsonify(error="No ThePornDB token set (Settings)."), 400
        import tpdb as tpdbmod
        try: p = tpdbmod.call_retry(f"/performers/{ident}", tok).get("data")
        except urllib.error.HTTPError as ex: return jsonify(error=f"ThePornDB error {ex.code}"), 502
        except Exception as ex: return jsonify(error=f"Could not reach ThePornDB ({type(ex).__name__})"), 502
        if not p: return jsonify(error="Performer not found on ThePornDB"), 404
        if str(p.get("id")) != str(row["tpdb_performer_id"] or ""):  # different performer: drop the cached scene list
            c.execute("UPDATE people SET tpdb_scenes_json=NULL, tpdb_scenes_at=NULL, tpdb_scenes_total=NULL WHERE id=?", (pid,))
        c.execute("UPDATE people SET tpdb_manual=1 WHERE id=?", (pid,))
        e = person_entry(c, row)
        apply_tpdb(c, c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(), p, allow_image=not (e["films"] & review_film_ids(c)),
                   fill_only=bool(e["films"] & classic_film_ids(c)) and (c.execute("SELECT source FROM people WHERE id=?", (pid,)).fetchone()[0] or "") != "manual")
        return jsonify(ok=True, name=p.get("name"))

    def backup_photo(name):
        """A replaced or removed photo file is moved to posters/people/backup/ (never deleted)."""
        if not name or os.path.dirname(name): return
        src = os.path.join(PHOTO_DIR, name)
        if not os.path.isfile(src): return
        bd = os.path.join(PHOTO_DIR, "backup"); os.makedirs(bd, exist_ok=True)
        dst = os.path.join(bd, name); n = 0
        while os.path.exists(dst): n += 1; dst = os.path.join(bd, f"{n}-{name}")
        try: os.replace(src, dst)
        except OSError: pass

    def save_photo(c, pid, data, ext):
        ext = ext.lower() if ext and ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
        os.makedirs(PHOTO_DIR, exist_ok=True)
        name = f"manual-{pid}-{int(time.time())}{ext}"; n = 0
        while os.path.exists(os.path.join(PHOTO_DIR, name)): n += 1; name = f"manual-{pid}-{int(time.time() * 1000)}-{n}{ext}"  # never overwrite a file
        old = c.execute("SELECT photo_file, photo_orig FROM people WHERE id=?", (pid,)).fetchone()
        with open(os.path.join(PHOTO_DIR, name), "wb") as fh: fh.write(data)
        ed = set(json.loads(c.execute("SELECT edited_fields FROM people WHERE id=?", (pid,)).fetchone()[0] or "[]")) | {"photo"}
        c.execute("UPDATE people SET photo_file=?, photo_orig=NULL, edited_fields=?, fetched_at=? WHERE id=?", (name, json.dumps(sorted(ed)), time.time(), pid)); c.commit()
        for x in (old or ()):  # the replaced hand photo (and its uncropped original) go to backup/; cached TMDB/ThePornDB files stay
            if x and x != name and x.startswith("manual-"): backup_photo(x)

    def photo_from_request(c, pid):
        f = request.files.get("photo")
        if f and f.filename: save_photo(c, pid, f.read(), os.path.splitext(f.filename)[1]); return None
        url = (request.form.get("photo_url") if request.form else None) or ((request.get_json(silent=True) or {}).get("photo_url"))
        if url:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "FilmTagger"}), timeout=20) as r: data = r.read(8 * 1024 * 1024)
            except Exception as ex: return f"Could not download the photo ({type(ex).__name__})"
            save_photo(c, pid, data, os.path.splitext(urllib.parse.urlparse(url).path)[1])
        return None

    @app.post("/api/people/new")
    def person_new():
        """Add a person by hand (form or JSON): name required; every field given is stored as a manual (protected) edit."""
        d = request.form.to_dict() if request.form else (request.get_json(silent=True) or {})
        name = " ".join((d.get("name") or "").split())
        if not name: return jsonify(error="Name is required"), 400
        c = C(); k = fold(name)
        r = c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()
        if not r:
            c.execute("INSERT INTO people(name, name_norm, source, tpdb_performer_id, tpdb_manual, tmdb_person_id, tmdb_manual, fetched_at) "
                      "VALUES(?,?,'manual',?,1,?,1,?)", (name, k, TPDB_NONE, NO_MATCH, time.time())); c.commit()
            r = c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()
        edited = set(json.loads(r["edited_fields"] or "[]"))
        for f in EDITABLE:
            if f in d and str(d[f] or "").strip():
                col = "links_json" if f == "links" else f
                c.execute(f"UPDATE people SET {col}=? WHERE id=?", (clean_value(f, d[f]), r["id"])); edited.add(col)
        c.execute("UPDATE people SET edited_fields=? WHERE id=?", (json.dumps(sorted(edited)), r["id"])); c.commit()
        err = photo_from_request(c, r["id"])
        return jsonify(ok=True, id=r["id"], name=r["name"], existed=bool(r["source"] != "manual" or r["fetched_at"] is None), warning=err,
                       art_export=art_export_person(c, r["id"]) if not err and (request.files.get("photo") or (request.form.get("photo_url") if request.form else None) or (request.get_json(silent=True) or {}).get("photo_url")) else None)

    def art_export_person(c, pid):
        """Copy the person's current photo into one video folder (artexport.py); never fails the request."""
        try:
            import artexport
            r = c.execute("SELECT name, photo_file FROM people WHERE id=?", (pid,)).fetchone()
            if not r or not r["photo_file"]: return dict(status="no local photo")
            return artexport.export_person(c, r["name"], os.path.join(PHOTO_DIR, r["photo_file"]))
        except Exception as e:
            return dict(status=f"error: {type(e).__name__}")

    @app.post("/api/person/<int:pid>/artwork/export")
    def person_art_export(pid):
        c = C()
        if not c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone(): return jsonify(error="Unknown person"), 404
        return jsonify(art_export=art_export_person(c, pid))

    @app.post("/api/person/<int:pid>/photo/upload")
    def person_photo_upload(pid):
        c = C()
        if not c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone(): return jsonify(error="Unknown person"), 404
        err = photo_from_request(c, pid)
        return (jsonify(error=err), 502) if err else jsonify(ok=True, art_export=art_export_person(c, pid))

    # ---------- profile photo crop: always cut from the full original (kept as <name>.orig.<ext>), Reset crop restores it ----------
    def photo_source(row):
        """Full-size photo to crop from: the kept original, else the current photo file (downloaded/cached if needed)."""
        if row["photo_orig"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_orig"])): return row["photo_orig"]
        if row["photo_file"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_file"])): return row["photo_file"]
        r = person_photo(row["id"])
        if isinstance(r, tuple): return None
        for n in os.listdir(PHOTO_DIR) if os.path.isdir(PHOTO_DIR) else []:
            tp = (photo_choice(row) or ("",))[0] == "tpdb"
            if tp:
                import hashlib
                if n.startswith("tpdb-" + hashlib.sha1(row["image_url"].encode()).hexdigest()[:16]): return n
            elif row["profile_path"] and n == f"person-{row['tmdb_person_id']}-{os.path.basename(row['profile_path'])}": return n
        return None

    @app.get("/api/person/<int:pid>/photo/orig")
    def person_photo_orig(pid):
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        n = row and photo_source(row)
        if not n: return jsonify(error="No photo"), 404
        return send_from_directory(PHOTO_DIR, n, max_age=0)

    @app.post("/api/person/<int:pid>/photo/crop")
    def person_photo_crop(pid):
        """{x, y, w, h} in pixels of the full original -> cropped JPEG/PNG becomes the photo (marked hand-edited)."""
        import cv2
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        src = photo_source(row)
        if not src: return jsonify(error="This person has no photo to crop"), 404
        with open(os.path.join(PHOTO_DIR, src), "rb") as fh: data = fh.read()
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None: return jsonify(error="Could not read the photo"), 400
        h, w = img.shape[:2]; d = request.get_json(silent=True) or {}
        try:
            x = min(max(0, int(round(float(d["x"])))), w - 1); y = min(max(0, int(round(float(d["y"])))), h - 1)
            cw = min(max(1, int(round(float(d["w"])))), w - x); ch = min(max(1, int(round(float(d["h"])))), h - y)
        except Exception: return jsonify(error="Bad crop box"), 400
        stem, ext = os.path.splitext(src)
        if stem.endswith(".orig"): stem = stem[:-5]
        orig = src if src == row["photo_orig"] else f"{stem}.orig{ext}"
        if orig != src:
            import shutil; shutil.copyfile(os.path.join(PHOTO_DIR, src), os.path.join(PHOTO_DIR, orig))
        png = ext.lower() == ".png"
        ok, buf = cv2.imencode(".png" if png else ".jpg", img[y:y + ch, x:x + cw], [] if png else [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok: return jsonify(error="Could not encode the cropped photo"), 500
        name = f"{stem}.crop-{int(time.time())}{'.png' if png else '.jpg'}"
        with open(os.path.join(PHOTO_DIR, name), "wb") as fh: fh.write(buf.tobytes())
        old = row["photo_file"]
        ed = set(json.loads(row["edited_fields"] or "[]")) | {"photo"}
        c.execute("UPDATE people SET photo_file=?, photo_orig=?, edited_fields=?, fetched_at=? WHERE id=?", (name, orig, json.dumps(sorted(ed)), time.time(), pid)); c.commit()
        if old and old not in (name, orig) and ".crop-" in old: backup_photo(old)  # moved to backup/, never deleted
        return jsonify(ok=True, width=cw, height=ch, orig_width=w, orig_height=h, photo=f"/api/person/{pid}/photo?v={int(time.time())}", art_export=art_export_person(c, pid))

    @app.post("/api/person/<int:pid>/photo/reset_crop")
    def person_photo_reset_crop(pid):
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        if not (row["photo_orig"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_orig"]))): return jsonify(error="The photo is not cropped"), 400
        old = row["photo_file"]
        c.execute("UPDATE people SET photo_file=?, photo_orig=NULL, fetched_at=? WHERE id=?", (row["photo_orig"], time.time(), pid)); c.commit()
        if old and ".crop-" in old: backup_photo(old)  # moved to backup/, never deleted
        return jsonify(ok=True, art_export=art_export_person(c, pid))

    @app.post("/api/person/<int:pid>/merge")
    def person_merge(pid):
        """Fold another person into this one: {other: person id | name}. The other's name and aliases become (protected)
        aliases here; their row is kept but points here (merged_into), so links and cast credits keep working."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        v = (request.json or {}).get("other")
        o = None
        if isinstance(v, int) or (isinstance(v, str) and v.strip().isdigit()):
            o = c.execute("SELECT * FROM people WHERE id=?", (int(v),)).fetchone()
        elif isinstance(v, str) and v.strip():
            o = ensure_row(c, v)
        if not o: return jsonify(error="Pick the person to merge"), 400
        if o["id"] == pid: return jsonify(error="That is the same person"), 400
        if row["merged_into"]: return jsonify(error="This person was merged into someone else"), 400
        try: als = json.loads(row["aliases"] or "[]")
        except Exception: als = []
        try: oal = json.loads(o["aliases"] or "[]")
        except Exception: oal = []
        have = {fold(a) for a in als} | {row["name_norm"]}
        for a in [o["name"]] + oal:
            if isinstance(a, str) and a.strip() and fold(a) not in have: als.append(" ".join(a.split())); have.add(fold(a))
        edited = set(json.loads(row["edited_fields"] or "[]")) | {"aliases"}
        if photo_locked(o) and o["photo_file"] and not photo_locked(row):  # the kept person takes over a hand-set photo instead of losing it
            edited.add("photo"); c.execute("UPDATE people SET photo_file=?, photo_orig=?, fetched_at=? WHERE id=?", (o["photo_file"], o["photo_orig"], time.time(), pid))
        c.execute("UPDATE people SET aliases=?, edited_fields=? WHERE id=?", (json.dumps(als), json.dumps(sorted(edited)), pid))
        if o["favorite"]:  # a starred duplicate: the kept person keeps the star (earliest date)
            c.execute("UPDATE people SET favorite=1, favorite_at=MIN(COALESCE(favorite_at, ?), ?) WHERE id=?", (o["favorite_at"] or time.time(), o["favorite_at"] or time.time(), pid))
            c.execute("UPDATE people SET favorite=0, favorite_at=NULL WHERE id=?", (o["id"],))
        c.execute("UPDATE people SET merged_into=? WHERE id=?", (pid, o["id"]))
        c.execute("UPDATE people SET merged_into=? WHERE merged_into=?", (pid, o["id"]))  # people merged into the other move along
        c.commit()
        return jsonify(ok=True, id=pid, merged=o["name"], aliases=als)

    @app.post("/api/person/<int:pid>/unmerge")
    def person_unmerge(pid):
        """Undo a merge for one person row that was merged into another (its name stays as an alias until removed by hand)."""
        c = C(); c.execute("UPDATE people SET merged_into=NULL WHERE id=?", (pid,)); c.commit(); return jsonify(ok=True)

    @app.post("/api/person/<int:pid>/rename")
    def person_rename(pid):
        """Change the person's primary name: {name}. The old name becomes an alias; the new name is
        removed from aliases if present. Film credits under the old name are rewritten. If the new
        name already exists as a separate person, refuses and suggests Merge."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        r, status = apply_rename(c, row, (request.json or {}).get("name"))
        if status != 200: return jsonify(r), status
        c.commit(); return jsonify(ok=True, **r)

    @app.post("/api/person/<int:pid>/edit")
    def person_edit(pid):
        """Hand edits to profile fields; edited fields are never overwritten by TPDB/TMDB fetches.
        `name` changes the primary name (same rules as /rename)."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        d = request.json or {}
        if "name" in d:
            r, status = apply_rename(c, row, d.get("name"))
            if status != 200: return jsonify(r), status
            row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        edited = set(json.loads(row["edited_fields"] or "[]"))
        for k in EDITABLE:
            if k not in d: continue
            v = d[k]
            col = "links_json" if k == "links" else k
            v = clean_value(k, v)
            note = " ".join(str(d.get("birth_date_note") or "").split())[:80] if col == "birthday" else ""
            # the same birth date typed again with a note (or not yet protected) is a confirmation: protect it and keep the note
            confirm = col == "birthday" and v and v == row[col] and (note or col not in edited)
            if v != row[col] or confirm:
                c.execute(f"UPDATE people SET {col}=? WHERE id=?", (v, pid)); edited.add(col)
                if col == "ethnicity": c.execute("UPDATE people SET ethnicity_source=? WHERE id=?", ("manual" if v else None, pid))
                if col == "birthday":  # optional note on where the date came from, e.g. "entered by Kyle"
                    c.execute("UPDATE people SET birth_date_source=? WHERE id=?", ((f"hand edit ({note})" if note else "hand edit") if v else None, pid))
                    if v:  # settled by hand: off the birth-date review list
                        try: c.execute("UPDATE birthdate_lookup SET status='skipped', birthday=?, source=?, detail='hand edit' WHERE name_norm=? AND status='ambiguous'",
                                       (v, f"hand edit ({note})" if note else "hand edit", row["name_norm"]))
                        except Exception: pass  # table not created yet
        c.execute("UPDATE people SET edited_fields=? WHERE id=?", (json.dumps(sorted(edited)), pid)); c.commit()
        return jsonify(ok=True, edited=sorted(edited))

    # ---------- performer photo galleries (persongallery.py): a folder linked in place, listed on every visit ----------
    import persongallery as pgmod, gallery as gallerymod
    PG_THUMBS = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "gallery_thumbs")

    def _pg_row(c, pid):
        row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(); hops = 0
        while row and row["merged_into"] and hops < 5:  # merged person: the gallery of the person it was merged into
            row = c.execute("SELECT * FROM people WHERE id=?", (row["merged_into"],)).fetchone(); hops += 1
        return row

    def _pg_review(c, row):
        """People credited in a title flagged 'review by hand': gallery hidden, like scene galleries and photos."""
        try: return bool(set(person_entry(c, row)["films"]) & review_film_ids(c))
        except Exception: return False

    def _pg_folder(pid):
        c = C(); row = _pg_row(c, pid)
        if not row: return None, (jsonify(error="Unknown person"), 404)
        if _pg_review(c, row): return None, (jsonify(error="Gallery hidden: this person is in a title flagged for review by hand"), 403)
        f = row["gallery_dir"]
        if not f: return None, (jsonify(error="No gallery folder set for this person"), 404)
        if not os.path.isdir(f): return None, (jsonify(error="Gallery folder not found: " + f, folder=f, missing=True), 404)
        return f, None

    @app.get("/api/person/<int:pid>/gallery")
    def person_gallery(pid):
        c = C(); row = _pg_row(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        f = row["gallery_dir"]
        empty = dict(count=0, images=[], groups=[], hidden_images=[], hidden_count=0)
        if not f: return jsonify(folder=None, **empty)
        if _pg_review(c, row): return jsonify(error="Gallery hidden: this person is in a title flagged for review by hand", folder=f, hidden=True), 403
        if not os.path.isdir(f): return jsonify(folder=f, missing=True, **empty)
        return jsonify(folder=f, **pgmod.listing(gallerymod.images(f), pgmod.hidden_set(c, row["id"])))

    @app.get("/api/person/<int:pid>/gallery/thumb")
    def person_gallery_thumb(pid):
        f, err = _pg_folder(pid)
        if err: return err
        p = gallerymod.safe_name(f, request.args.get("name"))
        if not p: return jsonify(error="not found"), 404
        from flask import send_file
        t = gallerymod.thumb(p, PG_THUMBS)
        return send_file(t or p, max_age=86400 * 30)

    @app.get("/api/person/<int:pid>/gallery/image")
    def person_gallery_image(pid):
        f, err = _pg_folder(pid)
        if err: return err
        p = gallerymod.safe_name(f, request.args.get("name"))
        if not p: return jsonify(error="not found"), 404
        from flask import send_file
        return send_file(p, max_age=86400)

    @app.post("/api/person/<int:pid>/gallery/folder")
    def person_gallery_set(pid):
        """{path}: link that folder (in place, never changed) as the person's gallery; empty path clears it."""
        c = C(); row = _pg_row(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        path = str((request.json or {}).get("path") or "").strip().strip('"').strip()
        if path:
            path = os.path.normpath(path)
            if not os.path.isdir(path): return jsonify(error="That folder doesn't exist: " + path), 400
        c.execute("UPDATE people SET gallery_dir=? WHERE id=?", (path or None, row["id"])); c.commit()
        return jsonify(ok=True, id=row["id"], gallery_dir=path or None, count=len(gallerymod.images(path)) if path else 0)

    @app.post("/api/person/<int:pid>/gallery/hide")
    def person_gallery_hide(pid):
        """{name, hidden}: hide or unhide one photo in this person's gallery. The file is never moved, renamed or deleted.
        hidden defaults to true. The hide list is keyed by person id + path relative to the gallery folder."""
        c = C(); row = _pg_row(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        if _pg_review(c, row): return jsonify(error="Gallery hidden: this person is in a title flagged for review by hand"), 403
        f = row["gallery_dir"]
        if not f or not os.path.isdir(f): return jsonify(error="No gallery folder set for this person"), 400
        d = request.json or {}
        name = pgmod.rel_path(d.get("name"))
        if not name or any(p in ("", ".", "..") for p in name.split("/")):
            return jsonify(error="Invalid photo name"), 400
        hide = d["hidden"] if "hidden" in d else True
        hide = hide not in (False, 0, "0", "false", "False")
        if hide:
            if not gallerymod.safe_name(f, name): return jsonify(error="Photo not found in this gallery"), 404
            pgmod.set_hidden(c, row["id"], name, True)
        else:
            pgmod.set_hidden(c, row["id"], name, False)
        c.commit()
        return jsonify(ok=True, folder=f, **pgmod.listing(gallerymod.images(f), pgmod.hidden_set(c, row["id"])))

    def _pg_adjust(pid, action):
        import galleryadjust as gamod
        f, err = _pg_folder(pid)
        if err: return err
        d = request.args if action == "info" else (request.get_json(silent=True) or {})
        name = d.get("name")
        r, status = gamod.handle(f, name, action, d)
        return jsonify(r), status

    @app.get("/api/person/<int:pid>/gallery/adjust")
    def person_gallery_adjust_info(pid):
        """Size and whether an `_originals` backup exists for ?name= (a gallery-safe relative path)."""
        return _pg_adjust(pid, "info")

    @app.post("/api/person/<int:pid>/gallery/adjust")
    def person_gallery_adjust_save(pid):
        """{name, contrast, brightness, gamma, sharpen}: apply adjustments to that gallery photo.
        The original is copied to `_originals` once (never deleted) before the first overwrite."""
        return _pg_adjust(pid, "save")

    @app.post("/api/person/<int:pid>/gallery/adjust/revert")
    def person_gallery_adjust_revert(pid):
        """{name}: put the kept original back. The backup in `_originals` is not deleted."""
        return _pg_adjust(pid, "revert")

    @app.post("/api/people/gallery_link")
    def people_gallery_link():
        """{root, apply}: match the root's subfolders (one per performer) to people by name/alias. apply=true links every
        exact/alias match whose person has no gallery yet; ambiguous and unmatched folders are only reported."""
        d = request.json or {}; root = os.path.normpath(str(d.get("root") or "").strip().strip('"'))
        if not d.get("root") or not os.path.isdir(root): return jsonify(error="Folder not found: " + root), 400
        c = C(); subs, loose = pgmod.survey(root)
        res = pgmod.match_folders([s["folder"] for s in subs], pgmod.people_for_match(c, adult_film_ids(c)))
        info = {s["folder"]: s for s in subs}
        for m in res:
            s = info[m["folder"]]; m.update(path=s["path"], count=s["count"], linked=False)
            if m["kind"] not in ("exact", "alias") or not d.get("apply"): continue
            pid = m["person_id"]
            if isinstance(pid, str): pid = ensure_row(c, m["candidates"][0]["name"])["id"]; m["person_id"] = pid
            cur = c.execute("SELECT gallery_dir FROM people WHERE id=?", (pid,)).fetchone()
            if cur and cur[0] and os.path.normcase(os.path.normpath(cur[0])) != os.path.normcase(s["path"]):
                m["note"] = "already has another gallery folder: " + cur[0]; continue
            c.execute("UPDATE people SET gallery_dir=? WHERE id=?", (s["path"], pid)); m["linked"] = True
        c.commit()
        return jsonify(root=root, folders=res, loose_images=loose,
                       totals=dict(folders=len(res), linked=sum(m["linked"] for m in res),
                                   **{k: sum(m["kind"] == k for m in res) for k in ("exact", "alias", "ambiguous", "none")}))

    @app.get("/api/person/<int:pid>/photo")
    def person_photo(pid):
        """Profile photo (TMDB or ThePornDB), cached locally under posters/people/ like posters."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if row and row["photo_file"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_file"])):
            return send_from_directory(PHOTO_DIR, row["photo_file"], max_age=86400 * 30)
        ch = photo_choice(row)
        if not ch: return jsonify(error="No photo"), 404
        tp = ch[0] == "tpdb"
        if tp:
            import hashlib
            ext = os.path.splitext(urllib.parse.urlparse(row["image_url"]).path)[1][:5] or ".jpg"
            name, src = f"tpdb-{hashlib.sha1(row['image_url'].encode()).hexdigest()[:16]}{ext}", row["image_url"]
        else:
            name, src = f"person-{row['tmdb_person_id']}-{os.path.basename(row['profile_path'])}", "https://image.tmdb.org/t/p/w300" + row["profile_path"]
        path = os.path.join(PHOTO_DIR, name)
        if not os.path.exists(path):
            try:
                with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "FilmTagger"}), timeout=15) as r: data = r.read()
                os.makedirs(PHOTO_DIR, exist_ok=True)
                with open(path + ".tmp", "wb") as fh: fh.write(data)
                os.replace(path + ".tmp", path)
            except Exception as ex: return jsonify(error=f"Photo unavailable: {ex}"), 502
        return send_from_directory(PHOTO_DIR, name, max_age=86400 * 30)

    # ---------- Classic Porn backfill: TMDB-primary photo + profile for everyone credited on a Classic Porn title ----------
    CPB = {}
    def classic_people(c):
        cl = classic_film_ids(c); out = []
        for row in c.execute("SELECT * FROM people WHERE merged_into IS NULL ORDER BY id").fetchall():
            e = person_entry(c, row)
            if e["films"] & cl: out.append((row, e))
        return out

    def classic_backfill(delay=0.3):
        st = CPB.update(running=True, started=time.time(), done=0, total=0, phase="scanning", error=None,
                        counts=dict(switched_to_tmdb_photo=0, already_tmdb=0, tmdb_no_photo_kept_tpdb=0, tmdb_no_photo_none=0,
                                    no_tmdb_match=0, ambiguous=0, skipped_hand_edits=0, errors=0), ambiguous=[], no_match=[], switched=[]) or CPB
        def work():
            c = C()
            try:
                c.execute(REVIEW_SCHEMA); key = key_of(c)
                if not key: st["error"] = "No TMDB key is set."; return
                ppl = classic_people(c); st.update(total=len(ppl), phase="updating"); n = st["counts"]
                for row, e in ppl:
                    st["done"] += 1
                    try:
                        if json.loads(row["edited_fields"] or "[]") or row["photo_file"] or row["photo_orig"] or row["source"] == "manual":
                            n["skipped_hand_edits"] += 1; continue
                        if row["source"] != "tpdb" and row["profile_path"] and (row["tmdb_person_id"] or 0) > 0:
                            n["already_tmdb"] += 1; continue
                        tid = row["tmdb_person_id"]
                        if tid is None or (tid == NO_MATCH and not row["tmdb_manual"]):
                            tid = classic_resolve(c, key, row, e["films"]); time.sleep(delay)
                            if tid is not None: c.execute("UPDATE people SET tmdb_person_id=? WHERE id=?", (tid, row["id"])); c.commit()
                        if tid is None:
                            n["ambiguous"] += 1; st["ambiguous"].append(dict(id=row["id"], name=row["name"])); continue
                        if tid <= 0:
                            n["no_tmdb_match"] += 1; st["no_match"].append(dict(id=row["id"], name=row["name"])); continue
                        had = row["image_url"] if row["source"] == "tpdb" else None
                        fetch(c, key, c.execute("SELECT * FROM people WHERE id=?", (row["id"],)).fetchone(), tmdb_primary=True); time.sleep(delay)
                        r2 = c.execute("SELECT * FROM people WHERE id=?", (row["id"],)).fetchone()
                        if r2["profile_path"]:
                            n["switched_to_tmdb_photo"] += 1; st["switched"].append(dict(id=row["id"], name=row["name"], old_tpdb_image=had))
                        elif r2["image_url"]: n["tmdb_no_photo_kept_tpdb"] += 1
                        else: n["tmdb_no_photo_none"] += 1
                    except urllib.error.HTTPError as ex:
                        if ex.code == 401: st["error"] = "TMDB key rejected"; return
                        n["errors"] += 1
                    except Exception: n["errors"] += 1
                CO_ITEMS.clear()
            finally:
                st.update(running=False, phase="done", finished=time.time())
        threading.Thread(target=work, daemon=True).start(); return st

    @app.post("/api/people/classic_backfill")
    def classic_backfill_start():
        if CPB.get("running"): return jsonify(CPB)
        return jsonify(classic_backfill(float((request.get_json(silent=True) or {}).get("delay", 0.3))))

    @app.get("/api/people/classic_backfill")
    def classic_backfill_status():
        c = C(); c.execute(REVIEW_SCHEMA)
        rv = [dict(id=r[0], name=r[1], candidates=json.loads(r[2] or "[]")) for r in c.execute("SELECT person_id, name, candidates_json FROM tmdb_match_review ORDER BY name")]
        return jsonify(dict(CPB, review=rv) if CPB else dict(running=False, review=rv))

    # ---------- Credited with (co-credits): what two or three people made together ----------
    CO = {}  # (ids, data key) -> (at, result)
    CO_ITEMS = {}  # (person id, data key) -> that person's known credits (library + cached filmographies)
    CO_ADULT_GENRES = ("hardcore", "softcore")
    def _bd(s):
        import datetime
        try: return datetime.date.fromisoformat(s[:10]) if s and len(s) >= 10 else None
        except ValueError: return None
    def _adult_ok(bd, rd, y):
        import reports as repmod
        return repmod._adult_ok(bd, rd, y)
    def co_canon(c, pid):
        row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(); hops = 0
        while row and row["merged_into"] and hops < 5:
            t = c.execute("SELECT * FROM people WHERE id=?", (row["merged_into"],)).fetchone()
            if not t: break
            row = t; hops += 1
        return row
    def co_keys(c, row, e):
        ks = {row["name_norm"]} | {fold(n) for n in e["names"]} | {fold(n) for n in db.expand_person(c, row["name"])}
        try: ks |= {fold(" ".join(a.split())) for a in json.loads(row["aliases"] or "[]") if isinstance(a, str)}
        except Exception: pass
        return {k for k in ks if k}
    def co_items(c, row, e, lib_tp):
        """Everything we know this person made: library films (L<id>), TMDB credits not in the library (T<tmdb id>),
        ThePornDB scenes not in the library (P<scene id>). Cached data only: no network."""
        items = {}
        for fid in e["films"]:
            items["L%d" % fid] = dict(film_id=fid, roles=sorted(ro for ro, s in e["roles"].items() if fid in s), xroles=[])
        if row["credits_json"] and (row["tmdb_person_id"] or 0) > 0:
            try: groups = filmography(c, json.loads(row["credits_json"]))
            except Exception: groups = []
            for g in groups:
                for x in g["credits"]:
                    k = "L%d" % x["film_id"] if x["film_id"] else "T%d" % x["tmdb_id"]
                    it = items.setdefault(k, dict(film_id=x["film_id"], roles=[], xroles=[]))
                    it.setdefault("tmdb_id", x["tmdb_id"]); it.setdefault("title", x["title"]); it.setdefault("year", x["year"]); it.setdefault("date", x["date"])
                    if x.get("url"): it.setdefault("url", x["url"])
                    if x.get("media_type") or x.get("tmdb_kind"): it.setdefault("media_type", x.get("media_type") or x.get("tmdb_kind"))
                    lab = "Actor" if g["dept"] == "Acting" else (x["role"] or g["dept"])
                    for part in (lab.split(" / ") if g["dept"] != "Acting" else [lab]):
                        if part and part not in it["xroles"]: it["xroles"].append(part)
        names = [row["name_norm"]] + [fold(n) for n in db.expand_person(c, row["name"])][1:]
        for r2 in c.execute(f"SELECT tpdb_scenes_json FROM people WHERE tpdb_scenes_json IS NOT NULL AND (id=? OR name_norm IN ({','.join('?' * len(names))}))", [row["id"]] + names):
            try: lst = json.loads(r2[0] or "[]")
            except Exception: lst = []
            for x in lst:
                ids = [str(x.get("id") or ""), str(x.get("_id") or "")]
                fid = next((lib_tp[i] for i in ids if i and i in lib_tp), None)
                k = "L%d" % fid if fid else "P%s" % ids[0]
                it = items.setdefault(k, dict(film_id=fid, roles=[], xroles=[]))
                if not fid:
                    it.update(title=x.get("title"), date=(x.get("date") or "")[:10], studio=x.get("studio"),
                              url=f"https://theporndb.net/scenes/{x.get('slug') or x.get('id')}")
                    it["year"] = int(it["date"][:4]) if it["date"][:4].isdigit() else None
                if "Performer" not in it["xroles"]: it["xroles"].append("Performer")
        return items

    def cocredit_data(c, pids):
        key = (tuple(pids), tuple(c.execute("SELECT COUNT(*), MAX(updated_at), SUM(COALESCE(rating,0)), SUM(COALESCE(watched,0)) FROM films").fetchone()),
               tuple(c.execute("SELECT COUNT(*), MAX(COALESCE(fetched_at,0)), MAX(COALESCE(tpdb_scenes_at,0)), SUM(LENGTH(COALESCE(birthday,''))), MAX(id) FROM people").fetchone()))
        hit = CO.get(key)
        if hit and time.time() - hit[0] < 120: return hit[1]
        lib_tp = {str(r[1]): r[0] for r in c.execute("SELECT id, tpdb_id FROM films WHERE tpdb_id IS NOT NULL AND tpdb_id<>''")}
        P = []
        for pid in pids:
            row = co_canon(c, pid)
            if not row: return None
            if any(p["row"]["id"] == row["id"] for p in P): continue
            e = person_entry(c, row)
            ik = (row["id"], key[1], key[2]); its = CO_ITEMS.get(ik)
            if its is None:
                its = CO_ITEMS[ik] = co_items(c, row, e, lib_tp)
                if len(CO_ITEMS) > 256: CO_ITEMS.pop(next(iter(CO_ITEMS)))
            P.append(dict(row=row, e=e, keys=co_keys(c, row, e), items=its, bd=_bd(row["birthday"])))
        if len(P) < 2: return dict(people=P, rows=[], excluded=[], ids=set(), error="Pick two different people.")
        adult_ids = adult_film_ids(c)
        anyadult = any(p["e"]["films"] & adult_ids for p in P)
        shared = set(P[0]["items"])
        for p in P[1:]: shared &= set(p["items"])
        fids = [int(k[1:]) for k in shared if k[0] == "L"]
        F = {}
        for i in range(0, len(fids), 500):
            ch = fids[i:i + 500]
            for r in c.execute(f"SELECT id, title, year, release_date, kind, studio, director, cast_, writers, producers, cinematographer, composer, primary_genre, country, rating, watched, runtime, tmdb_id FROM films WHERE id IN ({','.join('?' * len(ch))})", ch):
                F[r["id"]] = r
        rows, excluded = [], []
        for k in shared:
            it0 = P[0]["items"][k]
            if k[0] == "L":
                f = F.get(int(k[1:]))
                if not f: continue
                adult = f["id"] in adult_ids or f["kind"] == "scene" or (f["primary_genre"] or "").strip().lower() in CO_ADULT_GENRES
                r = dict(key=k, film_id=f["id"], title=f["title"], year=f["year"], date=f["release_date"] or "", kind=f["kind"] or "film",
                         studio=f["studio"] or "", director=f["director"] or "", genre=(f["primary_genre"] or "").strip(), country=f["country"] or "",
                         rating=f["rating"], watched=f["watched"], runtime=f["runtime"], tmdb_id=f["tmdb_id"], in_library=True, adult=adult)
            else:
                adult = k[0] == "P" or anyadult
                r = dict(key=k, film_id=None, title=it0.get("title") or "?", year=it0.get("year"), date=it0.get("date") or "", kind="scene" if k[0] == "P" else "film",
                         studio=it0.get("studio") or "", director="", genre="", country="", rating=None, watched=None, runtime=None,
                         tmdb_id=it0.get("tmdb_id"), url=it0.get("url") or tmdb_page_url(it0.get("tmdb_id"), it0.get("media_type") or it0.get("tmdb_kind")),
                         in_library=False, adult=adult)
            if r["year"] is None and (r["date"] or "")[:4].isdigit(): r["year"] = int(r["date"][:4])
            r["roles"] = []
            for p in P:
                it = p["items"][k]
                labs = [RLBL_CO.get(ro, ro) for ro in it["roles"]] or it["xroles"]
                if adult and r["kind"] == "scene": labs = ["Performer" if x in ("Actor",) else x for x in labs]
                r["roles"].append(labs or ["Credited"])
            if adult:
                why = None
                for p in P:
                    if not p["bd"]: why = "no birth date for " + p["row"]["name"]; break
                    if not _adult_ok(p["bd"], r["date"] or None, r["year"]): why = "under 18 (or too close to tell) at release"; break
                if why:
                    excluded.append(dict(title=r["title"], year=r["year"], in_library=r["in_library"], why=why)); continue
            rows.append(r)
        rows.sort(key=lambda r: (r["year"] is None, r["date"] or (str(r["year"]) if r["year"] else ""), fold(r["title"] or "")))
        res = dict(people=P, rows=rows, excluded=excluded, ids={r["film_id"] for r in rows if r["film_id"]}, F=F, anyadult=anyadult)
        CO[key] = (time.time(), res)
        if len(CO) > 64: CO.pop(next(iter(CO)))
        return res

    def cocredit_ids(c, pids):
        d = cocredit_data(c, pids)
        return set() if not d else d["ids"]
    COCREDIT["ids"] = cocredit_ids

    def co_pids(s):
        out = []
        for x in str(s or "").split(","):
            x = x.strip()
            if x.isdigit() and int(x) not in out: out.append(int(x))
        return out[:3]

    @app.get("/api/people/cocredit")
    def people_cocredit():
        """Read-only: titles two or three people are credited on together (library + cached TMDB/ThePornDB filmographies),
        with stats. Adult titles count only when every compared person has a known birth date and was 18+ at release."""
        pids = co_pids(request.args.get("ids"))
        if len(pids) < 2: return jsonify(error="Give two or three person ids, e.g. ids=12,34"), 400
        c = C(); d = cocredit_data(c, pids)
        if d is None: return jsonify(error="Unknown person"), 404
        P = d["people"]
        def prof(p):
            row, e, its = p["row"], p["e"], p["items"]
            yrs = []
            for k, it in its.items():
                y = it.get("year")
                if y is None and k[0] == "L":
                    f = d.get("F", {}).get(it["film_id"])
                    y = f["year"] if f else None
                if y: yrs.append(y)
            if not yrs and e["films"]:
                yrs = [r[0] for r in c.execute(f"SELECT year FROM films WHERE year IS NOT NULL AND id IN ({','.join('?' * len(e['films']))})", list(e["films"]))]
            rated = [r[0] for r in c.execute(f"SELECT rating FROM films WHERE rating>0 AND id IN ({','.join('?' * len(e['films']))})", list(e["films"]))] if e["films"] else []
            fetched = bool(row["fetched_at"] or row["tpdb_fetched_at"] or row["tmdb_person_id"] == NO_MATCH or row["source"] == "manual")
            return dict(id=row["id"], name=row["name"], photo=photo_url(row), known_for=row["known_for"] or (ROLE_DEPT[top_role(e)] if e["roles"] else None),
                        birthday=row["birthday"], deathday=row["deathday"], career=[min(yrs), max(yrs)] if yrs else None,
                        credits=len(its), in_library=len(e["films"]), roles={ro: len(s) for ro, s in e["roles"].items()},
                        has_filmography=bool(row["credits_json"] or row["tpdb_scenes_json"]), fetched=fetched,
                        avg_rating=round(sum(rated) / len(rated), 2) if rated else None, rated=len(rated))
        out = dict(people=[prof(p) for p in P], error=d.get("error"))
        if request.args.get("lite"): return jsonify(people=[dict(id=x["id"], name=x["name"]) for x in out["people"]], in_library=len(d["ids"]))
        rows = d["rows"]; libr = [r for r in rows if r["in_library"]]
        from collections import Counter
        def top(cnt, n=12): return [dict(name=k, n=v) for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], fold(kv[0])))[:n]]
        dirs, stud, gen, cty = Counter(), Counter(), Counter(), Counter()
        selfk = [p["keys"] for p in P]
        dir_is = {}
        for r in rows:
            if r["studio"]: stud[r["studio"]] += 1
        for r in libr:
            for nm in (r["director"] or "").split(","):
                nm = " ".join(nm.split())
                if nm:
                    dirs[nm] += 1
                    for i, ks in enumerate(selfk):
                        if fold(nm) in ks: dir_is[nm] = i
            if r["genre"]: gen[r["genre"][:1].upper() + r["genre"][1:]] += 1
            for x in (r["country"] or "").split(","):
                x = x.strip()
                if x: cty[x] += 1
        # third people credited on the shared library titles (adult titles: performers with a known birth date, 18+ at release)
        allself = set().union(*selfk)
        ag = db.alias_groups(c)
        prow = {r["name_norm"]: r for r in c.execute("SELECT id, name, name_norm, birthday, merged_into FROM people")}
        also, also_name, also_role = Counter(), {}, {}
        for r in libr:
            f = d["F"][r["film_id"]]; seen = set()
            for ro, col in ROLE_COLS.items():
                if ro == "director": continue  # directors have their own card
                for nm in (f[col] or "").split(","):
                    nm = " ".join(nm.split()); k = fold(nm)
                    if not nm or k in allself: continue
                    g = ag.get(k); ck = fold(g[0]) if g else k
                    if ck in allself or ck in seen: continue
                    if r["adult"] and ro == "cast":
                        pr = None
                        for kk in ([k] + [fold(x) for x in (g or [])]):
                            if kk in prow and prow[kk]["birthday"]: pr = prow[kk]; break
                        bd = _bd(pr["birthday"]) if pr else None
                        if not bd or not _adult_ok(bd, r["date"] or None, r["year"]): continue
                    seen.add(ck); also[ck] += 1
                    also_name.setdefault(ck, Counter())[nm] += 1; also_role.setdefault(ck, Counter())[ro] += 1
        also_l = []
        for ck, n in sorted(also.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
            nm = also_name[ck].most_common(1)[0][0]; pr = prow.get(fold(nm)) or prow.get(ck); hops = 0
            while pr and pr["merged_into"] and hops < 5:
                t = next((x for x in prow.values() if x["id"] == pr["merged_into"]), None); hops += 1
                if not t: break
                pr = t
            also_l.append(dict(name=pr["name"] if pr else nm, pid=pr["id"] if pr else None, n=n, role=also_role[ck].most_common(1)[0][0]))
        watched_tracked = bool(c.execute("SELECT 1 FROM films WHERE watched=1 LIMIT 1").fetchone())
        rated = [r["rating"] for r in libr if r["rating"] and r["rating"] > 0]
        rts = [r["runtime"] for r in libr if r["runtime"]]
        excl = d["excluded"]
        notes = []
        if excl:
            nb = [p["row"]["name"] for p in P if not p["bd"]]
            if nb: notes.append(f"{' and '.join(nb)} {'has' if len(nb) == 1 else 'have'} no known birth date, so adult titles are left out ({len(excl)}).")
            else: notes.append(f"{len(excl)} adult title{'s' if len(excl) != 1 else ''} left out: someone was under 18 (or too close to tell) at release.")
        for r in rows: r.pop("rating_hidden", None)
        out.update(rows=rows, total=len(rows), in_library=len(libr), not_in_library=len(rows) - len(libr),
                   excluded=len(excl), excluded_in_library=sum(1 for x in excl if x["in_library"]), excluded_rows=excl[:200], notes=notes,
                   directors=[dict(x, is_person=dir_is.get(x["name"])) for x in top(dirs)], studios=top(stud), genres=top(gen), countries=top(cty), also=also_l,
                   ratings=dict(avg=round(sum(rated) / len(rated), 2), n=len(rated)) if rated else None,
                   watched=dict(n=sum(1 for r in libr if r["watched"] == 1), of=len(libr)) if watched_tracked else None,
                   runtime=dict(avg=round(sum(rts) / len(rts)), total=sum(rts), n=len(rts)) if rts else None,
                   adult_rule=bool(d.get("anyadult")))
        return jsonify(out)

    @app.get("/api/people/cocredit/suggest")
    def people_cocredit_suggest():
        """People credited with person `pid`, best first by titles shared (same count as the comparison page); `q` matches names and aliases."""
        c = C(); row = co_canon(c, int(request.args.get("pid") or 0))
        if not row: return jsonify(error="Unknown person"), 404
        return jsonify(people=co_suggest(c, row, request.args.get("q") or "", min(int(request.args.get("limit", 10)), 50)))

    def co_suggest(c, row, qtext, lim, extra=4, roles_of=None, pool=False):
        e = person_entry(c, row); F = e["films"]; mine = co_keys(c, row, e)
        q = fold(qtext or "").split()
        idx = index(c); ag = db.alias_groups(c)
        cand = {}
        for k, x in idx.items():
            if k in mine: continue
            n = len(x["films"] & F)
            if not n and not q: continue
            names = ag.get(k) or [best_name(x)]
            if q and not any(all(w in fold(nm) for w in q) for nm in names + [k]): continue
            ck = fold(names[0]) if ag.get(k) else k
            if ck in mine: continue
            cur = cand.get(ck)
            if not cur: cand[ck] = cur = dict(names=set(), films=set(), roles=Counter_(), key=k)
            cur["names"].add(best_name(x)); cur["films"] |= (x["films"] & F)
            for ro, s in x["roles"].items(): cur["roles"][ro] += len(s & F) if F else len(s)
            cur["all"] = cur.get("all", 0) + len(x["films"])
        # same age rule as the results page: adult titles only count when both have a known birth date and were 18+
        adult_ids = adult_film_ids(c); my_bd = _bd(row["birthday"])
        FD = {}
        if F & adult_ids:
            fl = sorted(F)
            for i in range(0, len(fl), 500):
                ch = fl[i:i + 500]
                for r in c.execute(f"SELECT id, release_date, year, kind, primary_genre FROM films WHERE id IN ({','.join('?' * len(ch))})", ch):
                    if r["id"] in adult_ids or r["kind"] == "scene" or (r["primary_genre"] or "").strip().lower() in CO_ADULT_GENRES: FD[r["id"]] = r
        rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM people")}
        for v in cand.values():
            r = next((rows[fold(n)] for n in sorted(v["names"]) if fold(n) in rows), None)
            if r and r["merged_into"]: r = co_canon(c, r["id"])
            v["row"] = r
            ad = v["films"] & set(FD)
            if ad:
                bd = _bd(r["birthday"]) if r else None
                v["films"] = v["films"] - {i for i in ad if not (my_bd and bd and _adult_ok(my_bd, FD[i]["release_date"], FD[i]["year"]) and _adult_ok(bd, FD[i]["release_date"], FD[i]["year"]))}
        def sc(v):
            nm = min(v["names"], key=len); fk = fold(nm)
            exact = 0 if q and fk == " ".join(q) else 1 if q and all(any(t.startswith(w) for t in fk.split()) for w in q) else 2
            return (-len(v["films"]), exact, -v.get("all", 0), fk)
        rk = (lambda v: v["roles"].get("director", 0) > 0) if roles_of == "director" else (lambda v: bool(v["roles"]) and max(v["roles"].items(), key=lambda kv: kv[1])[0] != "director") if roles_of == "others" else (lambda v: True)
        hits = sorted((v for v in cand.values() if (v["films"] or q) and rk(v)), key=sc)[:lim * 3]
        out, seen = [], set()
        for v in hits:
            r = v["row"]
            if r and r["id"] in seen: continue
            if r: seen.add(r["id"])
            nm = r["name"] if r else max(v["names"], key=len)
            role = max(v["roles"].items(), key=lambda kv: kv[1])[0] if v["roles"] else "cast"
            out.append(dict(name=nm, pid=r["id"] if r else None, n=len(v["films"]), total=len(v["films"]), role=role,
                            known_for=(r["known_for"] if r and r["known_for"] else ROLE_DEPT.get(role)), aka=sorted(n for n in v["names"] if n != nm)[:3],
                            _sc=sc(v)))
            if len(out) >= lim + extra: break
        # exact numbers for the ones shown: the same set the comparison page counts (library + cached filmographies, age rule)
        for x in out:
            if not x["pid"]: continue
            d = cocredit_data(c, [row["id"], x["pid"]])
            if d and not d.get("error"): x["n"], x["total"] = len(d["ids"]), len(d["rows"])
        out.sort(key=lambda x: (-x["total"], -x["n"], x["_sc"][1:]))
        out = [x for x in out if x["total"] or q]
        if not pool: out = out[:lim]  # pool: keep the whole exact-counted pool so the caller can pick its top `lim` by either count
        for x in out: x.pop("_sc")
        return out

    # ---------- Person page insights (Dashboard-style summary above the filmography) ----------
    INS = {}  # (person id, data key) -> (at, result)
    def co_dkey(c):
        return (tuple(c.execute("SELECT COUNT(*), MAX(updated_at), SUM(COALESCE(rating,0)), SUM(COALESCE(watched,0)) FROM films").fetchone()),
                tuple(c.execute("SELECT COUNT(*), MAX(COALESCE(fetched_at,0)), MAX(COALESCE(tpdb_scenes_at,0)), SUM(LENGTH(COALESCE(birthday,''))), MAX(id) FROM people").fetchone()))
    def psig(row):
        """This person's own data version: a ThePornDB scene list saved part-way (every 5 pages, tpdb_scenes_at=0) leaves the
        global data key unchanged, so caches built from a person's credits also key on these."""
        return (row["tpdb_scenes_at"], len(row["tpdb_scenes_json"] or ""), row["fetched_at"], len(row["credits_json"] or ""))
    def scenes_loading_now(pid):
        st = SCN.get(pid)
        return bool(st and st.get("running"))
    def person_rows(c, row, dk=None):
        """One person's credits (library + cached TMDB/ThePornDB filmography) as rows. Adult titles where they performed
        only count with a known birth date and 18+ at release (same rule as Reports / Credited with)."""
        dk = dk or co_dkey(c); e = person_entry(c, row)
        ik = (row["id"], dk[0], dk[1], psig(row)); its = CO_ITEMS.get(ik)
        if its is None:
            lib_tp = {str(r[1]): r[0] for r in c.execute("SELECT id, tpdb_id FROM films WHERE tpdb_id IS NOT NULL AND tpdb_id<>''")}
            its = CO_ITEMS[ik] = co_items(c, row, e, lib_tp)
            if len(CO_ITEMS) > 256: CO_ITEMS.pop(next(iter(CO_ITEMS)))
        adult_ids = adult_film_ids(c); anyadult = bool(e["films"] & adult_ids); bd = _bd(row["birthday"])
        fids = [int(k[1:]) for k in its if k[0] == "L"]; F = {}
        for i in range(0, len(fids), 500):
            ch = fids[i:i + 500]
            for r in c.execute(f"SELECT id, title, year, release_date, kind, studio, director, cast_, writers, producers, primary_genre, country, rating FROM films WHERE id IN ({','.join('?' * len(ch))})", ch):
                F[r["id"]] = r
        # any adult title in the library (adult folder, scene or adult genre) makes their titles outside it count as adult too
        anyadult = anyadult or any(f["kind"] == "scene" or (f["primary_genre"] or "").strip().lower() in CO_ADULT_GENRES for f in F.values())
        rows, excluded, xrows = [], 0, []
        for k, it in its.items():
            if k[0] == "L":
                f = F.get(int(k[1:]))
                if not f: continue
                adult = f["id"] in adult_ids or f["kind"] == "scene" or (f["primary_genre"] or "").strip().lower() in CO_ADULT_GENRES
                r = dict(film_id=f["id"], year=f["year"], date=f["release_date"] or "", kind=f["kind"] or "film", studio=f["studio"] or "",
                         genre=(f["primary_genre"] or "").strip(), country=f["country"] or "", rating=f["rating"], in_library=True, adult=adult)
            else:
                adult = k[0] == "P" or anyadult
                r = dict(film_id=None, year=it.get("year"), date=it.get("date") or "", kind="scene" if k[0] == "P" else "film", studio=it.get("studio") or "",
                         genre="", country="", rating=None, in_library=False, adult=adult, tmdb_id=it.get("tmdb_id") if k[0] == "T" else None,
                         media_type=it.get("media_type") or it.get("tmdb_kind"), url=it.get("url"))
            if r["year"] is None and (r["date"] or "")[:4].isdigit(): r["year"] = int(r["date"][:4])
            labs = [RLBL_CO.get(ro, ro) for ro in it["roles"]] or it["xroles"] or ["Credited"]
            if adult and r["kind"] == "scene": labs = ["Performer" if x == "Actor" else x for x in labs]
            r["roles"] = labs
            if adult and any(x in ("Actor", "Performer") for x in labs) and not (bd and _adult_ok(bd, r["date"] or None, r["year"])):
                excluded += 1; r["excluded"] = True; xrows.append(r); continue
            rows.append(r)
        return rows, excluded, e, F, xrows
    COCREDIT["person"] = lambda c, pid: {r["film_id"] for r in person_rows(c, co_canon(c, pid))[0] if r["film_id"]} if co_canon(c, pid) else set()

    def stable_want(roles, mode):
        if mode == "directed": return "Director" in roles
        return any(x == "Director" or "Writer" in x or "Screenplay" in x or "Story" in x or "Producer" in x for x in roles)

    def stable_tiers(c, row, e, films, missing_bd=None):
        """Stable (4) / Muses (5-7) / Harem (8+): performers in the films this person directed ('directed') or
        directed/wrote/produced ('all'). films: dicts with roles, cast (names), adult, date, year, in_library.
        Adult titles only count performers with a known birth date who were 18+ at release. `missing_bd`
        (dict) collects performers left out only for lacking a birth date: canonical key -> (name, count)."""
        from collections import Counter
        ag = db.alias_groups(c); mine = co_keys(c, row, e)
        prow = {r["name_norm"]: r for r in c.execute("SELECT id, name, name_norm, birthday, merged_into FROM people")}
        byid = {r["id"]: r for r in prow.values()}
        def canon_row(k, g):
            pr = next((prow[kk] for kk in [k] + [fold(x) for x in (g or [])] if kk in prow), None); hops = 0
            while pr and pr["merged_into"] and hops < 5:
                nx = byid.get(pr["merged_into"]); hops += 1
                if not nx: break
                pr = nx
            return pr
        out_all = {}
        for mode in ("directed", "all"):
            cnt, inl, nm, prs, nob = Counter(), Counter(), {}, {}, Counter()
            for r in films:
                if not stable_want(r["roles"], mode): continue
                seen = set()
                for n in r["cast"]:
                    n = " ".join((n or "").split()); k = fold(n)
                    if not n or k in mine: continue
                    g = ag.get(k); ckk = fold(g[0]) if g else k
                    if ckk in mine or ckk in seen: continue
                    if ckk not in prs: prs[ckk] = canon_row(k, g)
                    pr = prs[ckk]
                    if r["adult"]:
                        bd = _bd(pr["birthday"]) if pr else None
                        if not bd:
                            seen.add(ckk); nob[ckk] += 1; nm.setdefault(ckk, Counter())[n] += 1; continue
                        if not _adult_ok(bd, r["date"] or None, r["year"]): continue
                    seen.add(ckk); cnt[ckk] += 1; inl[ckk] += bool(r["in_library"]); nm.setdefault(ckk, Counter())[n] += 1
            out = []
            for ckk, n in sorted(cnt.items(), key=lambda kv: (-kv[1], -inl[kv[0]], kv[0])):
                if n < 4: break
                pr = prs.get(ckk)
                out.append(dict(name=pr["name"] if pr else nm[ckk].most_common(1)[0][0], pid=pr["id"] if pr else None, n=n, lib=inl[ckk],
                                tier="harem" if n >= 8 else "muses" if n >= 5 else "stable"))
                if len(out) >= 80: break
            out_all[mode] = out
            if missing_bd is not None:
                for ckk, k2 in nob.items():
                    if cnt[ckk] + k2 >= 4: missing_bd[ckk] = (nm[ckk].most_common(1)[0][0], cnt[ckk] + k2)
        out_all["films"] = dict(directed=sum(1 for r in films if stable_want(r["roles"], "directed")),
                                all=sum(1 for r in films if stable_want(r["roles"], "all")))
        out_all["films_lib"] = dict(directed=sum(1 for r in films if r["in_library"] and stable_want(r["roles"], "directed")),
                                    all=sum(1 for r in films if r["in_library"] and stable_want(r["roles"], "all")))
        return out_all

    # ---------- Stable / Muses / Harem over the whole filmography: TMDB casts of films not in the library (cached) ----------
    CAST_SCHEMA = "CREATE TABLE IF NOT EXISTS tmdb_movie_cast(tmdb_id INTEGER PRIMARY KEY, cast_json TEXT, at REAL)"
    CAST_KIND_SCHEMA = "CREATE TABLE IF NOT EXISTS tmdb_title_cast(tmdb_id INTEGER NOT NULL, tmdb_kind TEXT NOT NULL DEFAULT 'movie', cast_json TEXT, at REAL, PRIMARY KEY(tmdb_id, tmdb_kind))"
    def ensure_title_cast(c):
        c.execute(CAST_SCHEMA); c.execute(CAST_KIND_SCHEMA)
        if not c.execute("SELECT 1 FROM tmdb_title_cast LIMIT 1").fetchone():
            c.execute("INSERT OR IGNORE INTO tmdb_title_cast(tmdb_id, tmdb_kind, cast_json, at) SELECT tmdb_id, 'movie', cast_json, at FROM tmdb_movie_cast")
            c.commit()
    SJOB = {}   # canonical person id -> progress of the on-demand job
    SALL = {}   # (person id, data key, cached casts) -> result

    def stable_all_films(c, row):
        """(films for stable_tiers incl. not-in-library TMDB titles with a cached cast, TMDB ids still missing a cast, e)."""
        rows, _, e, F, xrows = person_rows(c, row)
        need = sorted({(r["tmdb_id"], tmdb_title_kind(item=r)) for r in rows + xrows if not r["in_library"] and r.get("tmdb_id") and stable_want(r["roles"], "all")})
        cache = {}
        ensure_title_cast(c)
        for i in range(0, len(need), 500):
            ch = need[i:i + 500]
            for t, knd, cj in c.execute(
                f"SELECT tmdb_id, tmdb_kind, cast_json FROM tmdb_title_cast WHERE (tmdb_id, tmdb_kind) IN ({','.join('(?,?)' * len(ch))})",
                [v for pair in ch for v in pair]):
                try: cache[(t, tmdb_title_kind(knd))] = json.loads(cj or "[]")
                except Exception: cache[(t, tmdb_title_kind(knd))] = []
        films = []
        for r in rows + [x for x in xrows]:
            if r["in_library"]: films.append(dict(r, cast=(F[r["film_id"]]["cast_"] or "").split(",")))
            else:
                ck = (r.get("tmdb_id"), tmdb_title_kind(item=r))
                if ck in cache: films.append(dict(r, cast=[x[0] for x in cache[ck]], tmdb_cast=cache[ck]))
        return films, [pair for pair in need if pair not in cache], e

    def stable_all_job(pid):
        st = SJOB[pid] = dict(running=True, phase="filmography", done=0, total=0, errors=0, error=None, started=time.time())
        def work():
            c = C()
            try:
                ensure_title_cast(c)
                row = co_canon(c, pid); key = key_of(c)
                if not key: st["error"] = "No TMDB key is set, so only library titles can be counted."; return
                if not row["credits_json"] and (row["tmdb_person_id"] or 0) > 0:
                    fetch(c, key, row, keep_profile=(row["source"] or "") in ("tpdb", "manual")); CO_ITEMS.clear(); row = co_canon(c, pid)
                films, missing, e = stable_all_films(c, row)
                st.update(phase="casts", total=len(missing), done=0)
                for t, kind in missing:
                    time.sleep(0.25)  # ~4 TMDB requests a second
                    try: m = load_title_credits(tmdb, key, t, kind)
                    except urllib.error.HTTPError as ex:
                        if ex.code == 401: st["error"] = "TMDB rejected the API key (401)."; return
                        if ex.code != 404:
                            st["errors"] += 1
                            if st["errors"] > 20: st["error"] = f"TMDB kept failing (HTTP {ex.code})."; return
                            continue
                        m = {"cast": []}
                    except Exception:
                        st["errors"] += 1
                        if st["errors"] > 20: st["error"] = "Could not reach TMDB."; return
                        continue
                    cast = [[x.get("name"), x.get("id")] for x in (m.get("cast") or [])[:10] if x.get("name")]  # top-billed 10, like library titles
                    c.execute("INSERT OR REPLACE INTO tmdb_title_cast(tmdb_id, tmdb_kind, cast_json, at) VALUES(?,?,?,?)", (t, kind, json.dumps(cast), time.time())); c.commit()
                    st["done"] += 1
                # performers left out only for lacking a birth date (adult titles): TMDB person birthday, else Wikidata/Wikipedia
                films, missing, e = stable_all_films(c, row); mb = {}
                stable_tiers(c, row, e, films, mb)
                if mb:
                    import birthdates as bdm
                    tid = {}
                    for f in films:
                        for nmx, i in f.get("tmdb_cast") or []:
                            if nmx and i: tid.setdefault(fold(" ".join(nmx.split())), i)
                    todo = sorted(mb.items(), key=lambda kv: -kv[1][1])[:40]
                    st.update(phase="birthdays", total=len(todo), done=0)
                    for ckk, (nmx, _) in todo:
                        try:
                            if c.execute("SELECT 1 FROM birthdate_lookup WHERE name_norm=? AND status<>'error'", (fold(nmx),)).fetchone():
                                st["done"] += 1; continue
                        except Exception: bdm.ensure(c)
                        res = None
                        if tid.get(fold(nmx)):
                            time.sleep(0.25)
                            try:
                                bd = ((tmdb(f"person/{tid[fold(nmx)]}", key) or {}).get("birthday") or "")[:10]
                                if re.match(r"^\d{4}-\d{2}-\d{2}$", bd): res = dict(status="filled", birthday=bd, source=f"tmdb person {tid[fold(nmx)]}", detail="TMDB profile (cast of a film they made with this director)")
                            except Exception: res = None
                        if res:
                            if not bdm.apply(c, nmx, res): res = dict(res, status="skipped")
                            bdm._record(c, nmx, res); c.commit()
                        elif bdm.enabled():
                            try: bdm.lookup_one(c, nmx)
                            except Exception: pass
                        st["done"] += 1
            except Exception as ex:
                st["error"] = f"{type(ex).__name__}: {str(ex)[:200]}"
            finally:
                st.update(running=False, finished=time.time()); c.close()
        threading.Thread(target=work, daemon=True).start()
        return st

    @app.get("/api/people/<int:pid>/stable_all")
    def person_stable_all(pid):
        """Stable / Muses / Harem over the person's whole filmography (library + TMDB). Loads missing casts on demand."""
        c = C(); ensure_title_cast(c); row = co_canon(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        st = SJOB.get(row["id"])
        if st and st.get("running"):
            return jsonify(status="loading", phase=st["phase"], done=st["done"], total=st["total"])
        films, missing, e = stable_all_films(c, row)
        fresh = st and time.time() - st.get("finished", 0) < 600
        if not fresh and (missing or (not row["credits_json"] and (row["tmdb_person_id"] or 0) > 0) or request.args.get("refresh")):
            st = stable_all_job(row["id"])
            return jsonify(status="loading", phase=st["phase"], done=0, total=0)
        dk = co_dkey(c); n_cast = c.execute("SELECT COUNT(*) FROM tmdb_title_cast").fetchone()[0]
        ck = (row["id"], dk, n_cast); hit = SALL.get(ck)
        if hit is None:
            mb = {}
            hit = SALL[ck] = dict(stable_tiers(c, row, e, films, mb), no_birthday=len(mb))
            if len(SALL) > 64: SALL.pop(next(iter(SALL)))
        return jsonify(status="ok", stable=hit, missing_casts=len(missing), error=(st or {}).get("error"),
                       has_filmography=bool(row["credits_json"] and (row["tmdb_person_id"] or 0) > 0))

    @app.get("/api/people/<int:pid>/insights")
    def person_insights(pid):
        """Read-only summary for the person page: stats, credits per year, worked with most, genres/studios/countries,
        and for directors the Stable (4 films) / Muses (5-7) / Harem (8+) of performers they directed. Cached."""
        c = C(); row = co_canon(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        dk = co_dkey(c); ck = (row["id"], dk, psig(row))
        loading = scenes_loading_now(row["id"])  # ThePornDB scene list still loading: figures are partial, the page asks again
        hit = INS.get(ck)
        if hit and time.time() - hit[0] < 600 and not request.args.get("force"): return jsonify(dict(hit[1], cached=True, loading=loading))
        rows, excluded, e, F, xrows = person_rows(c, row, dk)
        from collections import Counter
        lib = [r for r in rows if r["in_library"]]
        yrs = Counter(); yl = Counter()
        for r in rows:
            if r["year"]: yrs[r["year"]] += 1; yl[r["year"]] += r["in_library"]
        busy = max(yrs.items(), key=lambda kv: (kv[1], -kv[0])) if yrs else None
        if busy and busy[1] < 2: busy = None  # no year with two or more credits
        def top(cnt, n=10): return [dict(name=k, n=v) for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], fold(kv[0])))[:n]]
        gen, stu, cty, rol = Counter(), Counter(), Counter(), Counter()
        for r in lib:
            if r["genre"]: gen[r["genre"][:1].upper() + r["genre"][1:]] += 1
            if r["studio"]: stu[r["studio"]] += 1
            for x in (r["country"] or "").split(","):
                if x.strip(): cty[x.strip()] += 1
        for r in rows:
            for l in r["roles"]: rol[l] += 1
        rated = [r["rating"] for r in lib if r["rating"] and r["rating"] > 0]
        # Worked with most: the page sorts by in-library or total count (its switch) and shows the top costars_n / directors_n
        costars = co_suggest(c, row, "", 8, extra=4, roles_of="others", pool=True)
        directors = [x for x in co_suggest(c, row, "", 5, extra=3, roles_of="director", pool=True) if x["total"]]
        # Stable / Muses / Harem: performers in films they directed (or directed/wrote/produced), library titles only
        stable = None
        if e["roles"].get("director"):
            dlib = lib + [r for r in xrows if r["in_library"]]  # a director who also appears in their own film still directed it
            films = [dict(r, cast=(F[r["film_id"]]["cast_"] or "").split(",")) for r in dlib]
            stable = stable_tiers(c, row, e, films)
        # credits withheld by the age rule: say how many and why, so the totals above never shrink silently
        excluded_lib = sum(1 for r in xrows if r["in_library"])
        excluded_why = None if not excluded else ("under_18" if _bd(row["birthday"]) else "no_birthday")
        res = dict(id=row["id"], name=row["name"], total=len(rows), in_library=len(lib), not_in_library=len(rows) - len(lib), excluded=excluded,
                   excluded_lib=excluded_lib, excluded_why=excluded_why,
                   adult=any(r["adult"] for r in rows) or bool(excluded), birthday=row["birthday"],
                   career=[min(yrs), max(yrs)] if yrs else None, busiest=dict(year=busy[0], n=busy[1], lib=yl[busy[0]]) if busy else None,
                   years=[dict(k=y, lib=yl[y], other=yrs[y] - yl[y]) for y in sorted(yrs)], undated=sum(1 for r in rows if not r["year"]),
                   top_genre=top(gen, 1)[0] if gen else None, top_role=top(rol, 1)[0] if rol else None, roles=top(rol, 8),
                   rating=dict(avg=round(sum(rated) / len(rated), 2), n=len(rated)) if rated else None,
                   genres=top(gen), studios=top(stu), countries=top(cty), costars=costars, directors=directors, costars_n=8, directors_n=5, stable=stable)
        if not loading:  # never cache figures from a part-loaded scene list
            INS[ck] = (time.time(), res)
            if len(INS) > 128: INS.pop(next(iter(INS)))
        return jsonify(dict(res, cached=False, loading=loading))

    # ---------- built / not-yet-opened profiles (people credited on adult titles: Porn Scenes + Classic Porn) ----------
    # Built = the person page has its career data: a ThePornDB scene list (scene performers, loaded when the page is first
    # opened) or, for Classic Porn people (TMDB primary), a TMDB filmography (page opened or the Classic Porn backfill).
    BUILT = {}  # (key, lib) -> (at, rows of /api/people/built)
    def is_built(row, classic):
        return bool(row["tpdb_scenes_json"]) or bool(classic and row["credits_json"] and (row["tmdb_person_id"] or 0) > 0)
    def adult_people(c, lib=""):
        """[(people row, classic?)] for everyone credited on an adult title (optionally only under folder lib)."""
        data = index(c); adult = adult_film_ids(c); cl = classic_film_ids(c)
        keys = {k for k, e in data.items() if e["films"] & adult}
        if lib: keys &= {k for k in lib_scope(c, data, lib) if k}
        out = []
        for row in c.execute("SELECT * FROM people WHERE merged_into IS NULL").fetchall():
            if row["name_norm"] in keys: out.append((row, bool(data[row["name_norm"]]["films"] & cl)))
        return out
    def mainstream_built(c, lib=""):
        """[(people row, False)] for Mainstream built profiles: credited in the library, on no adult title (the Porn rule
        above), with a TMDB filmography loaded (person page opened)."""
        data = index(c); adult = adult_film_ids(c)
        keys = {k for k, e in data.items() if e["films"] and not (e["films"] & adult)}
        if lib: keys &= {k for k in lib_scope(c, data, lib) if k}
        return [(row, False) for row in c.execute("SELECT * FROM people WHERE merged_into IS NULL AND credits_json IS NOT NULL AND tmdb_person_id>0").fetchall()
                if row["name_norm"] in keys]
    def career_years(row):
        ys = [int(y) for y in re.findall(r"(?<!\d)(19[2-9]\d|20[0-4]\d)(?!\d)", row["career"] or "")]
        if not ys: return None, None
        first = min(ys); last = max(ys)
        if re.search(r"present", row["career"] or "", re.I) or (len(ys) == 1 and not re.search(r"until", row["career"] or "", re.I)):
            last = max(last, time.localtime().tm_year)
        if re.search(r"until", row["career"] or "", re.I) and len(ys) == 1: first = None
        return first, last

    @app.get("/api/people/built")
    def people_built():
        """Built profiles with the same per-year counts as the person page graph (person_rows: library + ThePornDB + TMDB,
        age rule applied). lib = library folder. Cached until the films or a built person's data change (10 min at most)."""
        c = C(); dk = co_dkey(c)
        lib = (request.args.get("lib") or "").strip().rstrip("\\/")
        cat = "mainstream" if request.args.get("cat") == "mainstream" else "porn"  # Performers page: Mainstream / Porn buttons
        ppl = mainstream_built(c, lib) if cat == "mainstream" else [(r, k) for r, k in adult_people(c, lib) if is_built(r, k)]
        nf = c.execute("SELECT COUNT(*) FROM films").fetchone()[0]
        key = (nf, tuple((r["id"], r["tpdb_scenes_at"], r["fetched_at"], r["birthday"]) for r, _ in ppl))
        key = (cat,) + key
        hit = BUILT.get((key, lib))
        if hit is None or time.time() - hit[0] > 600:
            # per-person figures persist in people_built_stats; only people whose data changed are recomputed
            from collections import Counter
            c.execute("CREATE TABLE IF NOT EXISTS people_built_stats(person_id INTEGER PRIMARY KEY, k TEXT, data TEXT)")
            have = {r[0]: (r[1], r[2]) for r in c.execute("SELECT person_id, k, data FROM people_built_stats")}
            out, new = [], []
            with db.batch_cache():  # alias / index checks once, not once per person
                for row, _ in ppl:
                    pk = json.dumps([nf, row["tpdb_scenes_at"], row["fetched_at"], row["birthday"], len(row["credits_json"] or ""), len(row["tpdb_scenes_json"] or ""), row["name"]])
                    if row["id"] in have and have[row["id"]][0] == pk: out.append(json.loads(have[row["id"]][1])); continue
                    try: rows = person_rows(c, row, dk)[0]
                    except Exception: continue
                    yrs = Counter(r["year"] for r in rows if r["year"])
                    busy = max(yrs.items(), key=lambda kv: (kv[1], -kv[0])) if yrs else None
                    first, last = (min(yrs), max(yrs)) if yrs else (None, None)
                    span = (last - first + 1) if yrs else 0
                    d = dict(id=row["id"], name=row["name"], total=len(rows), dated=sum(yrs.values()),
                             busiest_year=busy[0] if busy else None, busiest_n=busy[1] if busy else 0, first=first, last=last,
                             years=span, avg=round(sum(yrs.values()) / span, 1) if span else None)
                    out.append(d)
                    new.append((row["id"], pk, json.dumps(d)))
            if new:  # one short write at the end: never hold the database write lock while computing
                c.executemany("INSERT OR REPLACE INTO people_built_stats VALUES(?,?,?)", new); c.commit()
            hit = BUILT[(key, lib)] = (time.time(), out)
            if len(BUILT) > 16: BUILT.pop(next(iter(BUILT)))
        # favorite star and photo are read fresh every time (not part of the cached figures): cheap columns only
        extra = {r["id"]: r for r in c.execute("SELECT id, favorite, photo_file, image_url, profile_path, source, fetched_at FROM people WHERE "
                                                "merged_into IS NULL AND (tpdb_scenes_json IS NOT NULL OR credits_json IS NOT NULL)")}
        def more(p):
            r = extra.get(p["id"])
            return dict(p, fav=bool(r and r["favorite"]), photo=photo_url(r) if r else None)
        return jsonify(people=[more(p) for p in hit[1]], total=len(hit[1]), cat=cat)

    # ---------- favorites: starred people (Porn Scenes > People > Favorites) ----------
    def fav_sort_key(name):
        """First name, then last name (accent/case-insensitive), then the full name."""
        w = db.fold_key(" ".join((name or "").split())).split()
        return (w[0] if w else "", w[-1] if len(w) > 1 else "", " ".join(w))

    @app.post("/api/people/<int:pid>/favorite")
    def person_favorite(pid):
        """{favorite: true|false}; no body = toggle. A person merged into someone else stars the person they were merged into.
        Only the favorite columns change, so hand-edited fields, photos and tags are never touched."""
        c = C(); row = c.execute("SELECT id, merged_into, favorite FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        hops = 0
        while row["merged_into"] and hops < 5:
            t = c.execute("SELECT id, merged_into, favorite FROM people WHERE id=?", (row["merged_into"],)).fetchone()
            if not t: break
            row = t; hops += 1
        d = request.get_json(silent=True) or {}
        v = d.get("favorite")
        fav = (not row["favorite"]) if v is None else (v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"))
        if bool(row["favorite"]) != fav:
            c.execute("UPDATE people SET favorite=?, favorite_at=? WHERE id=?", (1 if fav else 0, time.time() if fav else None, row["id"]))
        if not fav:  # rows merged into this person never keep them listed
            c.execute("UPDATE people SET favorite=0, favorite_at=NULL WHERE merged_into=? AND favorite=1", (row["id"],))
        c.commit()
        return jsonify(ok=True, id=row["id"], favorite=fav)

    @app.get("/api/people/favorites")
    def people_favorites():
        """Starred people A-Z by first name, then last name: photo, library scene count (and other library titles)."""
        c = C(); out = {}
        scene_ids = {r[0] for r in c.execute("SELECT id FROM films WHERE kind='scene'")}; adult_ids = adult_film_ids(c)
        for row in c.execute("SELECT * FROM people WHERE favorite=1").fetchall():
            hops = 0
            while row["merged_into"] and hops < 5:  # starred before a merge: list the kept person
                t = c.execute("SELECT * FROM people WHERE id=?", (row["merged_into"],)).fetchone()
                if not t: break
                row = t; hops += 1
            if row["id"] in out: continue
            films = person_entry(c, row)["films"]
            sc = len(films & scene_ids)
            out[row["id"]] = dict(id=row["id"], name=row["name"], photo=photo_url(row), scenes=sc, titles=len(films) - sc,
                                  added=row["favorite_at"], adult=bool(films & adult_ids))
        ppl = sorted(out.values(), key=lambda p: fav_sort_key(p["name"]))
        return jsonify(people=ppl, total=len(ppl))

    # ---------- Built profiles grouped by studio / label (or by site) ----------
    GRP = {}  # (by, lib, data version) -> result
    def label_map(c):
        """studio_key(site) -> its label: Kyle's own 'sub-label of' link first (Studios page), else the ThePornDB parent/network
        stored for the studio (studio_info.tpdb_parent, filled when its studio page was looked up)."""
        m = {}
        try:
            for k, par in c.execute("SELECT key, tpdb_parent FROM studio_info WHERE COALESCE(TRIM(tpdb_parent),'')<>''"): m[k] = par.strip()
        except Exception: pass
        for l in db.studio_links(c): m[db.studio_key(l["child"])] = l["parent"]
        return m
    def studio_group(name, saved_label, by, lm, am):
        """(group key, display name) for a scene's studio. by='label': follow labels up (3 levels at most); by='site': the site."""
        nm = " ".join((name or "").split())
        if not nm: return None, None
        nm = am.get(db.studio_key(nm), nm)  # hidden spellings -> main name
        if by == "label":
            hops, seen = 0, {db.studio_key(nm)}
            up = lm.get(db.studio_key(nm)) or saved_label
            while up and hops < 3:
                up = am.get(db.studio_key(up), " ".join(up.split()))
                if db.studio_key(up) in seen: break
                nm = up; seen.add(db.studio_key(nm)); hops += 1
                up = lm.get(db.studio_key(nm))
        return db.studio_key(nm), nm

    PT = {}  # person id -> (version, [(id, _id, studio, label, date)]): parsed ThePornDB scene lists (own + alias/merged rows)
    def tp_scenes(c, row):
        others = [fold(n) for n in db.expand_person(c, row["name"])][1:]
        orows = c.execute(f"SELECT id, tpdb_scenes_at, tpdb_scenes_json FROM people WHERE id<>? AND tpdb_scenes_json IS NOT NULL AND name_norm IN ({','.join('?' * len(others))})",
                          [row["id"]] + others).fetchall() if others else []
        ver = (row["tpdb_scenes_at"], len(row["tpdb_scenes_json"] or "")) + tuple((r[0], r[1], len(r[2])) for r in orows)
        hit = PT.get(row["id"])
        if hit and hit[0] == ver: return hit[1]
        out = []
        for js in [row["tpdb_scenes_json"]] + [r[2] for r in orows]:
            try: lst = json.loads(js or "[]")
            except Exception: lst = []
            for x in lst:
                out.append((str(x.get("id") or ""), str(x.get("_id") or ""), x.get("studio"), x.get("label"), (x.get("date") or "")[:10]))
        PT[row["id"]] = (ver, out)
        return out

    @app.get("/api/people/built_groups")
    def people_built_groups():
        """Built profiles grouped by studio/label (by=label, default) or ThePornDB site (by=site): every group with its number
        of distinct scenes (built performers only, age rule applied like the card figures) and the performers with at least
        GROUP_MIN scenes there (each performer's top group always). Cached until the people, films, studio links or aliases change."""
        by = "site" if request.args.get("by") == "site" else "label"
        c = C(); lib = (request.args.get("lib") or "").strip().rstrip("\\/")
        ppl = [r for r, k in adult_people(c, lib) if is_built(r, k)]
        lm = label_map(c) if by == "label" else {}
        am = db.studio_alias_map(c)
        ver = (by, lib, co_dkey(c), tuple((r["id"], r["tpdb_scenes_at"], len(r["tpdb_scenes_json"] or ""), r["birthday"]) for r in ppl),
               hash(frozenset(lm.items())), hash(frozenset(am.items())))
        hit = GRP.get(ver)
        if hit: return jsonify(dict(hit, cached=True))
        lib_tp = {str(r[0]): r[1] for r in c.execute("SELECT tpdb_id, id FROM films WHERE tpdb_id IS NOT NULL AND tpdb_id<>''")}
        FS = {r["id"]: r for r in c.execute("SELECT id, studio, release_date, year FROM films WHERE kind='scene'")}
        groups = {}  # key -> dict(names Counter, scenes set, people [(pid, n)])
        memo = {}
        def grp(studio, label):
            k = (studio, label)
            if k not in memo: memo[k] = studio_group(studio, label, by, lm, am)
            return memo[k]
        okc = {}
        def ok(bd, date, year):
            k = (bd, date, year)
            if k not in okc: okc[k] = _adult_ok(bd, date, year)
            return okc[k]
        with db.batch_cache():  # alias / index checks once for the whole loop
            for row in ppl:
                bd = _bd(row["birthday"])
                if not bd: continue  # no birth date: adult credits don't count (same as the card figures)
                cnt = Counter_(); seen = set()
                def add(sk, studio, label, date, year):
                    if sk in seen: return
                    seen.add(sk)
                    if not ok(bd, date, year): return
                    g, nm = grp(studio, label)
                    if not g: return
                    e_ = groups.get(g)
                    if e_ is None: e_ = groups[g] = dict(names=Counter_(), scenes=set(), people=[])
                    e_["names"][nm] += 1; e_["scenes"].add(sk); cnt[g] += 1
                for fid in sorted(person_entry(c, row)["films"]):
                    f = FS.get(fid)
                    if f: add("L%d" % fid, (f["studio"] or "").split(",")[0], None, f["release_date"], f["year"])
                for x in tp_scenes(c, row):  # (id, _id, studio, label, date)
                    fid = lib_tp.get(x[0]) or (x[1] and lib_tp.get(x[1]))
                    if fid and fid in FS:
                        f = FS[fid]; add("L%d" % fid, (f["studio"] or "").split(",")[0] or x[2], x[3], f["release_date"] or x[4], f["year"])
                    else:
                        add("P" + x[0], x[2], x[3], x[4], int(x[4][:4]) if x[4][:4].isdigit() else None)
                if not cnt: continue
                top = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]
                for g, n in cnt.items():
                    if n >= GROUP_MIN or g == top: groups[g]["people"].append((row["id"], n))
        out = []
        for g, e_ in groups.items():
            if not e_["people"]: continue
            out.append(dict(key=g, name=e_["names"].most_common(1)[0][0], scenes=len(e_["scenes"]),
                            people=[dict(id=i, n=n) for i, n in sorted(e_["people"], key=lambda t: -t[1])]))
        out.sort(key=lambda x: (-x["scenes"], db.fold_key(x["name"])))
        res = dict(by=by, min=GROUP_MIN, groups=out, total=len(out))
        GRP[ver] = res
        while len(GRP) > 4: GRP.pop(next(iter(GRP)))  # latest versions only (label / site, a folder or two)
        return jsonify(dict(res, cached=False))

    # ---------- Performers page: lightweight summary for the side panel ----------
    SUMC = {}  # (person id, data key, person version) -> result (never from a part-loaded scene list)
    @app.get("/api/people/<int:pid>/summary")
    def person_summary(pid):
        """Side-panel summary: photo, star, total credits and years active, busiest year, credits per year (library vs not),
        top studios (co-stars: /costars, a second request). Cached data only (no ThePornDB/TMDB calls), the same age rule as the person page.
        built=False means the profile was never opened: the panel can open the person page in the background to build it."""
        c = C(); row = co_canon(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        dk = co_dkey(c); ck = (row["id"], dk, psig(row), row["favorite"], row["photo_file"], row["image_url"])
        loading = scenes_loading_now(row["id"])
        hit = SUMC.get(ck)
        if hit and not request.args.get("force"): return jsonify(dict(hit, cached=True, loading=loading))
        e = person_entry(c, row)
        adult = bool(e["films"] & adult_film_ids(c)); classic = bool(e["films"] & classic_film_ids(c))
        with db.batch_cache():
            rows, excluded, _e, F, xrows = person_rows(c, row, dk)
        yrs, yl = Counter_(), Counter_()
        for r in rows:
            if r["year"]: yrs[r["year"]] += 1; yl[r["year"]] += r["in_library"]
        busy = max(yrs.items(), key=lambda kv: (kv[1], -kv[0])) if yrs else None
        am = db.studio_alias_map(c); st = Counter_(); names = {}
        for r in rows:
            for x in (r["studio"] or "").split(",")[:1]:
                x = " ".join(x.split())
                if not x: continue
                nm = am.get(db.studio_key(x), x); k = db.studio_key(nm); names.setdefault(k, nm); st[k] += 1
        studios = [dict(name=names[k], n=n) for k, n in sorted(st.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
        res = dict(id=row["id"], name=row["name"], photo=photo_url(row), favorite=bool(row["favorite"]),
                   built=is_built(row, classic), adult=adult, total=len(rows), in_library=sum(1 for r in rows if r["in_library"]),
                   first=min(yrs) if yrs else None, last=max(yrs) if yrs else None,
                   busiest=dict(year=busy[0], n=busy[1]) if busy else None,
                   years=[dict(k=y, lib=yl[y], other=yrs[y] - yl[y]) for y in sorted(yrs)], undated=sum(1 for r in rows if not r["year"]),
                   studios=studios, excluded=excluded, birthday=row["birthday"], deathday=row["deathday"], ethnicity=row["ethnicity"], nationality=row["nationality"],
                   excluded_why=None if not excluded else ("under_18" if _bd(row["birthday"]) else "no_birthday"))
        if not loading:
            SUMC[ck] = res
            while len(SUMC) > 256: SUMC.pop(next(iter(SUMC)))
        return jsonify(dict(res, cached=False, loading=loading))

    COSC = {}  # (person id, data key, person version) -> top co-stars
    def top_costars(c, row, dk):
        """Top 5 co-stars (exact shared counts, library + cached filmographies, age rule). Reuses the person page's
        insights answer when it is already cached; otherwise co_suggest inside one batch (alias tables checked once)."""
        ck = (row["id"], dk, psig(row))
        if ck in COSC: return COSC[ck], True
        ins = INS.get(ck)
        if ins and ins[1].get("costars") is not None:
            out = [dict(name=x["name"], pid=x["pid"], n=x["total"]) for x in sorted(ins[1]["costars"], key=lambda x: (-x["total"], -x["n"]))[:5]]
        else:
            with db.batch_cache():
                out = [dict(name=x["name"], pid=x["pid"], n=x["total"]) for x in co_suggest(c, row, "", 5, extra=0, roles_of="others")][:5]
        if not scenes_loading_now(row["id"]):
            COSC[ck] = out
            while len(COSC) > 256: COSC.pop(next(iter(COSC)))
        return out, False
    @app.get("/api/people/<int:pid>/costars")
    def person_costars(pid):
        """Second request of the Performers side panel (the slow part, kept out of /summary so the panel opens at once)."""
        c = C(); row = co_canon(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        try: out, hit = top_costars(c, row, co_dkey(c))
        except Exception: out, hit = [], False
        return jsonify(id=row["id"], costars=out, cached=hit, loading=scenes_loading_now(row["id"]))

    @app.get("/api/people/unopened")
    def people_unopened():
        """People on adult titles whose profile is not built yet, with the career years / scene total ThePornDB gave the
        background career-info job (or an earlier lookup). lib = library folder."""
        c = C(); lib = (request.args.get("lib") or "").strip().rstrip("\\/")
        out = []
        for row, k in adult_people(c, lib):
            if is_built(row, k): continue
            first, last = career_years(row)
            out.append(dict(id=row["id"], name=row["name"], career=row["career"], first=first, last=last,
                            years=(last - first + 1) if first and last else None, scenes=row["tpdb_scenes_total"],
                            looked_up=row["tpdb_performer_id"] is not None, found=row["tpdb_performer_id"] not in (None, TPDB_NONE), fav=bool(row["favorite"])))
        return jsonify(people=out, total=len(out), job={k: v for k, v in CJ.items() if k != "thread"})

    # ---------- Recently added (Performers row): cast of the titles added to the library most recently ----------
    @app.get("/api/people/recent")
    def people_recent():
        if request.args.get("cat") == "mix":  # empty Performers screen: Mainstream and Porn taken in turn, so it never reads as one category
            lim = max(1, min(int(request.args.get("limit", 16)), 40)); out = []
            m, p = (recent_list(c2, lim) for c2 in ("mainstream", "porn"))
            for i in range(lim):
                for l in (m, p):
                    if i < len(l) and len(out) < lim: out.append(l[i])
            return jsonify(people=out, cat="mix")
        return jsonify(people=recent_list(request.args.get("cat") or "", max(1, min(int(request.args.get("limit", 16)), 40))), cat=request.args.get("cat") or None)
    def recent_list(cat, lim):
        """Performers (cast) on the most recently added titles, newest first, one card per person with the date her latest
        title was added. cat = porn | mainstream (same rule as the Mainstream / Porn buttons) or empty for everyone."""
        c = C()
        data = index(c); adult = adult_film_ids(c)
        byname = {}; out = []; seen = set()
        for f in c.execute("SELECT id, cast_, added_at FROM films WHERE added_at IS NOT NULL AND COALESCE(cast_,'')<>'' ORDER BY added_at DESC, id DESC LIMIT 400"):
            for nm in (f["cast_"] or "").split(","):
                nm = " ".join(nm.split())
                if not nm: continue
                k = fold(nm)
                if k in seen: continue
                seen.add(k)
                e = data.get(k)
                is_adult = bool(e and e["films"] & adult)
                if cat == "porn" and not is_adult or cat == "mainstream" and is_adult: continue
                byname[k] = (nm, f["added_at"], is_adult)
                if len(byname) >= lim * 2: break
            if len(byname) >= lim * 2: break
        rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM people WHERE name_norm IN (%s)" % ",".join("?" * len(byname)), list(byname))} if byname else {}
        pids = set()
        for k, (nm, at, is_adult) in byname.items():
            r = rows.get(k)
            hops = 0
            while r is not None and r["merged_into"] and hops < 5:
                r = c.execute("SELECT * FROM people WHERE id=?", (r["merged_into"],)).fetchone(); hops += 1
            if r is None or r["id"] in pids: continue
            pids.add(r["id"])
            out.append(dict(id=r["id"], name=r["name"], photo=photo_url(r), fav=bool(r["favorite"]), adult=is_adult,
                            added=time.strftime("%Y-%m-%d", time.localtime(at))))
            if len(out) >= lim: break
        return out

    # ---------- Happy Birthday! / In memoriam (Performers rows): people credited in the library ----------
    ROWS_CACHE = {}
    def lib_people(c, cat, where):
        """(row-lite, is_adult) for un-merged people credited on a library title, filtered by Mainstream / Porn.
        Cached until the films index or any person's dates / merge change (keeps both rows well under 0.3 s)."""
        data = index(c); adult = adult_film_ids(c)
        mark = (IDX["key"], len(adult), tuple(c.execute("SELECT COUNT(*), TOTAL(CAST(REPLACE(birthday,'-','') AS INTEGER)), TOTAL(CAST(REPLACE(deathday,'-','') AS INTEGER)), TOTAL(merged_into), "
                                                       "MAX(id) FROM people WHERE " + where).fetchone()))
        ck = (cat, where); hit = ROWS_CACHE.get(ck)
        if hit and hit[0] == mark: return hit[1]
        out = []
        for r in c.execute("SELECT id, name, name_norm, birthday, deathday FROM people WHERE merged_into IS NULL AND " + where):
            e = data.get(r["name_norm"])
            if not e: continue
            a = bool(e["films"] & adult)
            if cat == "porn" and not a or cat == "mainstream" and a: continue
            out.append((dict(r), a))
        ROWS_CACHE[ck] = (mark, out)
        return out
    def _ymd(s):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", (s or "")[:10])
        if not m: return None
        try: return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError: return None
    def _bday_in(b, year):
        """b's birthday in `year`; Feb 29 falls on Feb 28 in non-leap years."""
        try: return datetime.date(year, b.month, b.day)
        except ValueError: return datetime.date(year, 2, 28)
    def _cards(c, picks):
        full = {r["id"]: r for r in c.execute("SELECT * FROM people WHERE id IN (%s)" % ",".join("?" * len(picks)), [p["id"] for p in picks])} if picks else {}
        for p in picks:
            r = full[p["id"]]; p.update(photo=photo_url(r), fav=bool(r["favorite"]))
        return picks
    @app.get("/api/people/birthdays")
    def people_birthdays():
        """Today's birthdays (local date) with the age turning; if none, the next 7 days. People with a death date are left out."""
        c = C(); cat = request.args.get("cat") or ""; today = datetime.date.today(); hits = []
        for r, a in lib_people(c, cat, "COALESCE(birthday,'')<>'' AND COALESCE(deathday,'')=''"):
            b = _ymd(r["birthday"])
            if not b or b.year < 1900 or today.year - b.year > 105: continue  # no death date on file but surely not living
            d = _bday_in(b, today.year)
            if d < today: d = _bday_in(b, today.year + 1)
            n = (d - today).days
            if n <= 7: hits.append(dict(id=r["id"], name=r["name"], adult=a, date=d.isoformat(), days=n, turns=d.year - b.year))
        mode = "today" if any(h["days"] == 0 for h in hits) else "week"
        hits = [h for h in hits if (h["days"] == 0) == (mode == "today")]
        hits.sort(key=lambda h: (h["days"], db.fold_key(h["name"])))
        return jsonify(mode=mode, today=today.isoformat(), people=_cards(c, hits[:40]), total=len(hits))
    @app.get("/api/people/memoriam")
    def people_memoriam():
        """People with a death date, most recent first: '1943–2010 · age 67'."""
        c = C(); cat = request.args.get("cat") or ""; hits = []
        for r, a in lib_people(c, cat, "COALESCE(deathday,'')<>''"):
            dd = _ymd(r["deathday"]); b = _ymd(r["birthday"])
            if not dd: continue
            age = (dd.year - b.year - ((dd.month, dd.day) < (b.month, b.day))) if b else None
            hits.append(dict(id=r["id"], name=r["name"], adult=a, deathday=dd.isoformat(), born=b.year if b else None, died=dd.year, age=age))
        hits.sort(key=lambda h: (h["deathday"], h["id"]), reverse=True)
        return jsonify(people=_cards(c, hits[:40]), total=len(hits))

    # ---------- death-date backfill (add-only): Wikidata P570 via TMDB person id (P4985) or a linked QID, then TMDB deathday ----------
    def _p570(where_vals, var, pred):
        import birthdates as bdm
        q = (f"SELECT ?k ?d ?pr ?occ WHERE {{ VALUES ?k {{ {where_vals} }} {pred} p:P570/psv:P570 [wikibase:timeValue ?d; wikibase:timePrecision ?pr]; "
             f"wdt:P106 ?occ . }}")
        res = {}
        for b in bdm._sparql(q)["results"]["bindings"]:
            if b["occ"]["value"].rsplit("/", 1)[1] not in bdm.FILM_OCC or int(b["pr"]["value"]) < 11: continue  # film people, day precision
            v = b["d"]["value"]
            if v.startswith("-"): continue
            res.setdefault(b["k"]["value"].rsplit("/", 1)[-1], set()).add(v[:10])
        return {k: next(iter(v)) for k, v in res.items() if len(v) == 1}  # one unambiguous date
    def _save_death(c, pid, d, src):
        n = c.execute("UPDATE people SET deathday=? WHERE id=? AND COALESCE(deathday,'')='' AND COALESCE(edited_fields,'') NOT LIKE '%\"deathday\"%'", (d, pid)).rowcount
        c.commit(); return n
    def death_wikidata(ids, gap=1.0, st=None):
        """Wikidata pass for people ids (batched SPARQL). Returns how many were filled."""
        c = C(); filled = 0
        try:
            rows = [r for r in c.execute("SELECT id, tmdb_person_id, birth_date_source FROM people WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)] if ids else []
            bytm = {str(r["tmdb_person_id"]): r["id"] for r in rows if (r["tmdb_person_id"] or 0) > 0}
            byq = {}
            for r in rows:
                m = re.match(r"wikidata (Q\d+)", r["birth_date_source"] or "")
                if m: byq[m.group(1)] = r["id"]
            for keys, mk, pred in ((list(bytm), lambda k: f'"{k}"', "?p wdt:P4985 ?k . ?p"), (list(byq), lambda k: "wd:" + k, "?k")):
                for i in range(0, len(keys), 150):
                    chunk = keys[i:i + 150]
                    try: got = _p570(" ".join(mk(k) for k in chunk), "k", pred)
                    except Exception:
                        if st is not None: st["errors"] += 1
                        time.sleep(10); continue
                    for k, d in got.items():
                        pid = (bytm if pred.startswith("?p") else byq).get(k)
                        if pid and _save_death(c, pid, d, "wikidata"): filled += 1; st is not None and st.__setitem__("wikidata", st["wikidata"] + 1)
                    if st is not None: st["wd_done"] += len(chunk)
                    time.sleep(gap)
        finally: c.close()
        return filled
    DJ = dict(running=False, phase=None, total=0, wd_done=0, tmdb_done=0, tmdb_total=0, wikidata=0, tmdb=0, errors=0, started=None, finished=None, error=None)
    def death_job(wait=0, tmdb_gap=0.3):
        def work():
            time.sleep(wait); c = C()
            try:
                data = index(c); key = key_of(c)
                todo = [r for r in c.execute("SELECT id, name_norm, tmdb_person_id, birth_date_source, edited_fields FROM people WHERE merged_into IS NULL AND COALESCE(deathday,'')='' "
                                             "AND (COALESCE(tmdb_person_id,0)>0 OR birth_date_source LIKE 'wikidata Q%')")
                        if r["name_norm"] in data and "deathday" not in json.loads(r["edited_fields"] or "[]")]
                DJ.update(total=len(todo), wd_done=0, tmdb_done=0, wikidata=0, tmdb=0, errors=0, started=time.time(), phase="wikidata")
                death_wikidata([r["id"] for r in todo], st=DJ)
                left = [r for r in todo if (r["tmdb_person_id"] or 0) > 0]
                left = [r for r in left if not (c.execute("SELECT deathday FROM people WHERE id=?", (r["id"],)).fetchone()[0] or "")]
                DJ.update(phase="tmdb", tmdb_total=len(left))
                for r in left:
                    DJ["tmdb_done"] += 1
                    if not key: break
                    try: d = (tmdb(f"person/{r['tmdb_person_id']}", key).get("deathday") or "")[:10]
                    except Exception: DJ["errors"] += 1; time.sleep(5); continue
                    if _ymd(d) and _save_death(c, r["id"], d, "tmdb"): DJ["tmdb"] += 1
                    time.sleep(tmdb_gap)
                db.set_setting(c, "death_backfill_done", time.time()); c.commit()
            except Exception as ex: DJ["error"] = str(ex)[:200]
            finally: DJ.update(running=False, phase="done", finished=time.time()); c.close()
        DJ.update(running=True, error=None); threading.Thread(target=work, daemon=True).start()
    if "pytest" not in __import__("sys").modules:
        _c = C()
        if not db.get_setting(_c, "death_backfill_done", None): death_job(wait=120)  # one-off, rate-limited, after start-up settles
    @app.get("/api/people/death_job")
    def death_job_status():
        c = C(); return jsonify(dict(DJ, with_death=c.execute("SELECT COUNT(*) FROM people WHERE merged_into IS NULL AND COALESCE(deathday,'')<>''").fetchone()[0]))
    @app.post("/api/people/death_job")
    def death_job_start():
        if not DJ.get("running"): death_job()
        return jsonify(DJ)

    # ---------- Ethnicity (add-only, never over a hand edit): Wikidata P172 via TMDB person id (P4985) or a linked QID,
    # then ThePornDB performer extras.ethnicity. Nothing is inferred; people with nothing published stay empty. ----------
    def _p172(vals, by_tmdb):
        import birthdates as bdm
        pat = ("?p wdt:P345 ?k . ?p wdt:P172 ?e ." if by_tmdb == "imdb" else "?p wdt:P4985 ?k . ?p wdt:P172 ?e ." if by_tmdb
               else "?k wdt:P172 ?e . BIND(?k AS ?p)")
        q = (f'SELECT ?k ?p ?e ?eLabel WHERE {{ VALUES ?k {{ {vals} }} {pat} '
             f'SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }} }}')
        res = {}
        for b in bdm._sparql(q)["results"]["bindings"]:
            k = b["k"]["value"].rsplit("/", 1)[-1]; lab = (b.get("eLabel") or {}).get("value") or ""
            if not lab or re.fullmatch(r"Q\d+", lab): continue  # no English label: skip rather than store an id
            qid = b["p"]["value"].rsplit("/", 1)[-1]
            res.setdefault(k, {}).setdefault(qid, set()).add(lab)  # original label; fold happens in resolve_ethnicity
        out = {}
        for k, items in res.items():
            if len(items) != 1: continue  # several Wikidata items for one TMDB id: ambiguous, skip
            qid, labs = next(iter(items.items())); labs = sorted(v for v in labs if v)
            value, conflict, listings = resolve_ethnicity(labs)
            if listings: out[k] = dict(value=value, source=f"wikidata {qid}", listings=listings, conflict=conflict)
        return out
    def _save_eth(c, pid, v, src, listings=None):
        if listings is not None:
            n = c.execute("UPDATE people SET ethnicity=?, ethnicity_source=?, ethnicity_checked_at=?, ethnicity_listings=? WHERE id=? AND COALESCE(ethnicity,'')='' "
                          "AND COALESCE(edited_fields,'') NOT LIKE '%\"ethnicity\"%'",
                          (v, src, time.time(), json.dumps(listings, ensure_ascii=False), pid)).rowcount
        else:
            n = c.execute("UPDATE people SET ethnicity=?, ethnicity_source=?, ethnicity_checked_at=? WHERE id=? AND COALESCE(ethnicity,'')='' "
                          "AND COALESCE(edited_fields,'') NOT LIKE '%\"ethnicity\"%'", (v, src, time.time(), pid)).rowcount
        c.commit(); return n
    def _apply_p172(c, pid, hit, src_suffix=""):
        """Save a resolved P172 hit (single group, raw labels, or a conflict queued for Needs ethnicity). Never over a hand edit."""
        src = hit["source"] + src_suffix
        listings = hit.get("listings") or []
        if hit.get("conflict"):
            n = c.execute("UPDATE people SET ethnicity_listings=?, ethnicity_source=?, ethnicity_checked_at=? WHERE id=? AND COALESCE(ethnicity,'')='' "
                          "AND COALESCE(edited_fields,'') NOT LIKE '%\"ethnicity\"%'",
                          (json.dumps(listings, ensure_ascii=False), src, time.time(), pid)).rowcount
            c.commit(); return "conflict" if n else None
        if hit.get("value") and _save_eth(c, pid, hit["value"], src, listings=listings):
            return "filled"
        return None
    def eth_wikidata(ids, gap=1.0, st=None, mark=()):
        """Wikidata P172 pass for people ids (batched SPARQL). `mark`: ids to flag as checked when their batch had nothing."""
        c = C(); filled = 0; mark = set(mark)
        try:
            rows = [r for r in c.execute("SELECT id, tmdb_person_id, birth_date_source FROM people WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)] if ids else []
            bytm = {str(r["tmdb_person_id"]): r["id"] for r in rows if (r["tmdb_person_id"] or 0) > 0}
            byq = {}
            for r in rows:
                m = re.match(r"wikidata (Q\d+)", r["birth_date_source"] or "")
                if m and r["id"] not in bytm.values(): byq[m.group(1)] = r["id"]
            for keys, mk, tm, by in ((list(bytm), lambda k: f'"{k}"', True, bytm), (list(byq), lambda k: "wd:" + k, False, byq)):
                for i in range(0, len(keys), 150):
                    chunk = keys[i:i + 150]
                    try: got = _p172(" ".join(mk(k) for k in chunk), tm)
                    except Exception:
                        if st is not None: st["errors"] += 1
                        time.sleep(10); continue
                    for k, hit in got.items():
                        if _apply_p172(c, by[k], hit) == "filled":
                            filled += 1
                            if st is not None: st["wikidata"] += 1
                    left = [by[k] for k in chunk if k not in got and by[k] in mark]
                    if left: c.execute("UPDATE people SET ethnicity_checked_at=? WHERE id IN (%s)" % ",".join("?" * len(left)), [time.time()] + left); c.commit()
                    if st is not None: st["wd_done"] += len(chunk)
                    time.sleep(gap)
            if st is not None:  # people with no Wikidata link at all count as looked at
                rest = [r["id"] for r in rows if r["id"] in mark and r["id"] not in bytm.values() and r["id"] not in byq.values()]
                if rest: c.execute("UPDATE people SET ethnicity_checked_at=? WHERE id IN (%s)" % ",".join("?" * len(rest)), [time.time()] + rest); c.commit()
        finally: c.close()
        return filled
    EJ = dict(running=False, phase=None, total=0, wd_done=0, wd_total=0, tpdb_done=0, tpdb_total=0, wikidata=0, tpdb=0, errors=0, started=None, finished=None, error=None,
              name_total=0, name_done=0, name_found=0, ext_total=0, ext_done=0, ext_found=0)
    def eth_by_name(c, tok, tpdbmod, gap=1.5):
        """Adult performers never linked to ThePornDB: search it by name. Only a single exact name / alias match is used, and
        never when its birth date disagrees with the one on file; the link itself is not stored (ethnicity only)."""
        adult = adult_film_ids(c); data = index(c)
        rows = [r for r in c.execute("SELECT id, name, name_norm, birthday, edited_fields FROM people WHERE merged_into IS NULL AND COALESCE(ethnicity,'')='' "
                                     "AND eth_name_checked_at IS NULL AND (tpdb_performer_id IS NULL OR tpdb_performer_id='')")
                if r["name_norm"] in data and data[r["name_norm"]]["films"] & adult and "ethnicity" not in json.loads(r["edited_fields"] or "[]")]
        EJ.update(phase="theporndb-name", name_total=len(rows) if tok else 0, name_done=0, name_found=0)
        for r in (rows if tok else []):
            EJ["name_done"] += 1
            try: res = tpdbmod.call_retry("/performers", tok, q=r["name"]).get("data") or []
            except Exception: EJ["errors"] += 1; time.sleep(5); continue
            t = fold(" ".join(r["name"].split())); nm = lambda x: fold(" ".join((x or "").split()))
            ex = [p for p in res if nm(p.get("name")) == t] or [p for p in res if any(nm(a) == t for a in (p.get("aliases") or []))]
            v = None
            if len(ex) == 1:
                p = ex[0]; pb = ((p.get("extras") or {}).get("birthday") or "")[:10]
                if not (r["birthday"] and pb and pb != r["birthday"][:10]):
                    v = norm_ethnicity((p.get("extras") or {}).get("ethnicity"))
                    if not v and not p.get("extras"):  # search results may be short: read the performer itself
                        try: v = norm_ethnicity(((tpdbmod.call_retry(f"/performers/{p['id']}", tok).get("data") or {}).get("extras") or {}).get("ethnicity")); time.sleep(gap)
                        except Exception: EJ["errors"] += 1
            if v and _save_eth(c, r["id"], v, f"theporndb {ex[0].get('id')} (name match)"): EJ["name_found"] += 1; EJ["tpdb"] += 1
            c.execute("UPDATE people SET eth_name_checked_at=? WHERE id=?", (time.time(), r["id"])); c.commit()
            time.sleep(gap)
    def eth_external(c, gap=0.3):
        """TMDB-linked people whose TMDB id is not on Wikidata: TMDB external_ids gives their Wikidata QID or IMDb id, then P172."""
        key = key_of(c)
        rows = [r for r in c.execute("SELECT id, tmdb_person_id, edited_fields FROM people WHERE merged_into IS NULL AND COALESCE(ethnicity,'')='' "
                                     "AND eth_ext_checked_at IS NULL AND COALESCE(tmdb_person_id,0)>0")
                if "ethnicity" not in json.loads(r["edited_fields"] or "[]")]
        EJ.update(phase="tmdb-external-ids", ext_total=len(rows) if key else 0, ext_done=0, ext_found=0)
        if not key: return
        for i in range(0, len(rows), 100):
            chunk = rows[i:i + 100]; byq, byimdb = {}, {}
            for r in chunk:
                EJ["ext_done"] += 1
                try: x = tmdb(f"person/{r['tmdb_person_id']}/external_ids", key)
                except Exception: EJ["errors"] += 1; time.sleep(3); continue
                if re.fullmatch(r"Q\d+", x.get("wikidata_id") or ""): byq[x["wikidata_id"]] = r["id"]
                elif re.fullmatch(r"nm\d+", x.get("imdb_id") or ""): byimdb[x["imdb_id"]] = r["id"]
                time.sleep(gap)
            for keys, tm, by in ((list(byq), False, byq), (list(byimdb), "imdb", byimdb)):
                if not keys: continue
                try: got = _p172(" ".join(("wd:" + k) if not tm else f'"{k}"' for k in keys), tm)
                except Exception: EJ["errors"] += 1; time.sleep(10); continue
                for k, hit in got.items():
                    if _apply_p172(c, by[k], hit, " (via IMDb id)" if tm else " (via TMDB)") == "filled":
                        EJ["ext_found"] += 1; EJ["wikidata"] += 1
                time.sleep(1.0)
            c.execute("UPDATE people SET eth_ext_checked_at=? WHERE id IN (%s)" % ",".join("?" * len(chunk)), [time.time()] + [r["id"] for r in chunk]); c.commit()
    def eth_job(wait=0, tpdb_gap=1.5):
        def work():
            time.sleep(wait); c = C()
            try:
                import tpdb as tpdbmod
                tok = db.get_setting(c, "tpdb_key", "") or ""
                todo = [r for r in c.execute("SELECT id, tmdb_person_id, tpdb_performer_id, birth_date_source, edited_fields FROM people WHERE merged_into IS NULL "
                                             "AND COALESCE(ethnicity,'')='' AND ethnicity_checked_at IS NULL AND (COALESCE(tmdb_person_id,0)>0 "
                                             "OR birth_date_source LIKE 'wikidata Q%' OR (tpdb_performer_id IS NOT NULL AND tpdb_performer_id<>?))", (TPDB_NONE,))
                        if "ethnicity" not in json.loads(r["edited_fields"] or "[]")]
                tp = lambda r: r["tpdb_performer_id"] not in (None, "", TPDB_NONE)
                wd = [r["id"] for r in todo if (r["tmdb_person_id"] or 0) > 0 or (r["birth_date_source"] or "").startswith("wikidata Q")]
                EJ.update(total=len(todo), wd_total=len(wd), wd_done=0, tpdb_done=0, wikidata=0, tpdb=0, errors=0, started=time.time(), phase="wikidata")
                eth_wikidata(wd, st=EJ, mark=[r["id"] for r in todo if not (tp(r) and tok)])
                left = [r for r in todo if tp(r) and not c.execute("SELECT COALESCE(ethnicity,'') FROM people WHERE id=?", (r["id"],)).fetchone()[0]]
                EJ.update(phase="theporndb", tpdb_total=len(left) if tok else 0)
                for r in (left if tok else []):
                    EJ["tpdb_done"] += 1
                    try: p = tpdbmod.call_retry(f"/performers/{r['tpdb_performer_id']}", tok).get("data") or {}
                    except Exception: EJ["errors"] += 1; time.sleep(5); continue
                    v = norm_ethnicity((p.get("extras") or {}).get("ethnicity"))
                    if v and _save_eth(c, r["id"], v, f"theporndb {r['tpdb_performer_id']}"): EJ["tpdb"] += 1
                    else: c.execute("UPDATE people SET ethnicity_checked_at=? WHERE id=?", (time.time(), r["id"])); c.commit()
                    time.sleep(tpdb_gap)
                eth_by_name(c, tok, tpdbmod, tpdb_gap)
                eth_external(c)
                db.set_setting(c, "ethnicity_backfill_done", time.time()); db.set_setting(c, "ethnicity_backfill_done_v2", time.time()); c.commit()
            except Exception as ex: EJ["error"] = str(ex)[:200]
            finally: EJ.update(running=False, phase="done", finished=time.time()); c.close()
        EJ.update(running=True, error=None); threading.Thread(target=work, daemon=True).start()
    if "pytest" not in __import__("sys").modules:
        _c = C()  # a backfill cut short by a restart picks up where it stopped (already-checked people are skipped)
        apply_ancestry_fold(_c)
        if db.get_setting(_c, "ethnicity_backfill_started", None) and not db.get_setting(_c, "ethnicity_backfill_done_v2", None): eth_job(wait=150)
    COV = {}
    def eth_counts(c):
        data = index(c); adult = adult_film_ids(c); vals = {}; unknown = 0; srcs = {}; known = 0
        cov = {"porn": {"with": 0, "without": 0}, "mainstream": {"with": 0, "without": 0}}; COV.clear(); COV.update(cov)
        for r in c.execute("SELECT name_norm, ethnicity, ethnicity_source FROM people WHERE merged_into IS NULL"):
            e = data.get(r["name_norm"])
            if not e: continue
            a = bool(e["films"] & adult); cov["porn" if a else "mainstream"]["with" if r["ethnicity"] else "without"] += 1
            if not r["ethnicity"]: unknown += 1; continue
            known += 1
            for v in eth_parts(r["ethnicity"]):  # someone listed as 'Black, White' counts under both
                d = vals.setdefault(v, dict(value=v, n=0, porn=0, mainstream=0, main=v in ETHNICITIES))
                d["n"] += 1; d["porn" if a else "mainstream"] += 1
            s = (r["ethnicity_source"] or "unknown").split(" ")[0]; srcs[s] = srcs.get(s, 0) + 1
        return sorted(vals.values(), key=lambda d: (not d["main"], -d["n"], d["value"])), unknown, srcs, known
    @app.get("/api/people/ethnicities")
    def people_ethnicities():
        """Ethnicity values held by people credited in the library, with counts (all / porn / mainstream)."""
        c = C()
        if request.args.get("ids"):  # ids of everyone holding one value (the People page narrows every tab with it)
            want = request.args["ids"]
            return jsonify(value=want, ids=[r["id"] for r in c.execute("SELECT id, ethnicity FROM people WHERE merged_into IS NULL AND COALESCE(ethnicity,'')<>''")
                                             if want in eth_parts(r["ethnicity"])])
        v, unknown, srcs, known = eth_counts(c)
        return jsonify(values=v, people_with=known, unknown=unknown, sources=srcs, main=ETHNICITIES)
    @app.get("/api/people/ethnicity_job")
    def eth_job_status():
        c = C(); v, unknown, srcs, known = eth_counts(c)
        return jsonify(dict(EJ, with_ethnicity=known, unknown=unknown, sources=srcs, coverage=dict(COV)))

    # ---------- Needs ethnicity (People → Mainstream / Porn): quick hand tagging, one person at a time ----------
    # People with no ethnicity and no hand edit, plus Wikidata conflicts (several main groups: listings shown, not auto-assigned).
    # Conflicts first, then photo, then most library credits. Tagging goes through Edit profile (a protected hand edit,
    # source 'manual'); Skip is remembered (ethnicity_skipped_at) until reset. Nothing is guessed.
    @app.get("/api/people/needs_ethnicity")
    def needs_ethnicity():
        c = C(); cat = "mainstream" if request.args.get("cat") == "mainstream" else "porn"
        lim = min(int(request.args.get("limit", 60)), 500)
        data = index(c); adult = adult_film_ids(c); out = []; skipped = 0
        for row in c.execute("SELECT * FROM people WHERE merged_into IS NULL AND COALESCE(ethnicity,'')=''").fetchall():
            e = data.get(row["name_norm"])
            if not e or not e["films"] or bool(e["films"] & adult) != (cat == "porn"): continue
            if "ethnicity" in json.loads(row["edited_fields"] or "[]"): continue  # cleared by hand on purpose
            if row["ethnicity_skipped_at"]: skipped += 1; continue
            n = len(e["films"] & adult) if cat == "porn" else len(e["films"])
            listings = parse_eth_listings(row)
            out.append(dict(id=row["id"], name=row["name"], photo=photo_url(row), nationality=row["nationality"], n=n, fav=bool(row["favorite"]),
                            listings=listings, conflict=bool(listings)))
        out.sort(key=lambda p: (not p["conflict"], not p["photo"], not p["fav"], -p["n"], db.fold_key(p["name"])))
        return jsonify(people=out[:lim], remaining=len(out), skipped=skipped, cat=cat, groups=ETHNICITIES)

    @app.post("/api/people/<int:pid>/eth_skip")
    def eth_skip(pid):
        c = C(); skip = (request.json or {}).get("skip", True)
        c.execute("UPDATE people SET ethnicity_skipped_at=? WHERE id=?", (time.time() if skip else None, pid)); c.commit()
        return jsonify(ok=True, skipped=bool(skip))

    @app.post("/api/people/eth_skips/reset")
    def eth_skips_reset():
        c = C(); cat = (request.json or {}).get("cat")
        if cat in ("porn", "mainstream"):
            data = index(c); adult = adult_film_ids(c)
            ids = [r["id"] for r in c.execute("SELECT id, name_norm FROM people WHERE ethnicity_skipped_at IS NOT NULL")
                   if r["name_norm"] in data and bool(data[r["name_norm"]]["films"] & adult) == (cat == "porn")]
        else: ids = [r["id"] for r in c.execute("SELECT id FROM people WHERE ethnicity_skipped_at IS NOT NULL")]
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]; c.execute("UPDATE people SET ethnicity_skipped_at=NULL WHERE id IN (%s)" % ",".join("?" * len(ch)), ch)
        c.commit(); return jsonify(ok=True, reset=len(ids))

    @app.post("/api/people/<int:pid>/eth_undo")
    def eth_undo(pid):
        """Undo a quick tag: only a value set by hand ('manual') is cleared, and the person goes back to the queue."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        if (row["ethnicity_source"] or "") not in ("manual", "hand edit"): return jsonify(error="Not a hand-set ethnicity"), 400
        ed = [k for k in json.loads(row["edited_fields"] or "[]") if k != "ethnicity"]
        c.execute("UPDATE people SET ethnicity=NULL, ethnicity_source=NULL, edited_fields=? WHERE id=?", (json.dumps(sorted(ed)), pid)); c.commit()
        return jsonify(ok=True)
    @app.post("/api/people/ethnicity_job")
    def eth_job_start():
        c = C(); db.set_setting(c, "ethnicity_backfill_started", time.time()); c.commit()
        if not EJ.get("running"): eth_job()
        return jsonify(EJ)

    # ---------- Needs attention (Performers tab): people missing key info ----------
    # Porn: everyone credited on an adult title, most scenes first (library scenes or ThePornDB total, whichever is larger).
    # Mainstream: only opened (built) profiles; the thousands of never-opened cast members would all lack everything.
    @app.get("/api/people/needs_attention")
    def people_needs_attention():
        c = C(); cat = "mainstream" if request.args.get("cat") == "mainstream" else "porn"
        data = index(c); adult = adult_film_ids(c); out = []
        rows = mainstream_built(c) if cat == "mainstream" else adult_people(c)
        for row, _ in rows:
            miss = []
            if not row["birthday"]: miss.append("birth")
            if not (row["photo_file"] or photo_choice(row)): miss.append("photo")
            if cat == "porn" and not row["gender"]: miss.append("gender")
            if cat == "porn" and row["tpdb_performer_id"] in (None, TPDB_NONE) and not (row["tmdb_person_id"] or 0) > 0: miss.append("link")
            if cat == "mainstream" and not (row["tmdb_person_id"] or 0) > 0: miss.append("link")
            if not miss: continue
            e = data.get(row["name_norm"]) or {"films": set()}
            lib = len(e["films"] & adult) if cat == "porn" else len(e["films"])
            out.append(dict(id=row["id"], name=row["name"], missing=miss, lib=lib, n=max(lib, row["tpdb_scenes_total"] or 0, len(json.loads(row["tpdb_scenes_json"])) if row["tpdb_scenes_json"] and not row["tpdb_scenes_total"] else 0) if cat == "porn" else lib,
                            photo=photo_url(row), fav=bool(row["favorite"])))
        out.sort(key=lambda p: (-p["n"], db.fold_key(p["name"])))
        counts = {k: sum(1 for p in out if k in p["missing"]) for k in ("birth", "photo", "gender", "link")}
        return jsonify(people=out, total=len(out), counts=counts, cat=cat)

    # ---------- gender backfill: add-only, from TMDB (person.gender) or else ThePornDB (performer extras.gender) ----------
    GJ = dict(running=False, done=0, total=0, filled=0, tmdb=0, tpdb=0, none=0, errors=0, started=None, finished=None, error=None)
    def gender_job(wait=0, tmdb_gap=0.25, tpdb_gap=1.5):
        def work():
            time.sleep(wait)
            import tpdb as tpdbmod
            c = C()
            try:
                key = key_of(c); tok = db.get_setting(c, "tpdb_key", "") or ""
                todo = [r for r in c.execute("SELECT * FROM people WHERE merged_into IS NULL AND COALESCE(gender,'')='' AND "
                                             "(COALESCE(tmdb_person_id,0)>0 OR (tpdb_performer_id IS NOT NULL AND tpdb_performer_id<>?))", (TPDB_NONE,)).fetchall()
                        if "gender" not in json.loads(r["edited_fields"] or "[]")]
                GJ.update(total=len(todo), done=0, filled=0, tmdb=0, tpdb=0, none=0, errors=0, started=time.time())
                for row in todo:
                    GJ["done"] += 1; g = src = None
                    try:
                        if (row["tmdb_person_id"] or 0) > 0 and key:
                            g = TMDB_GENDER.get(tmdb(f"person/{row['tmdb_person_id']}", key).get("gender")); src = "tmdb"; time.sleep(tmdb_gap)
                        if not g and row["tpdb_performer_id"] not in (None, TPDB_NONE) and tok:
                            p = tpdbmod.call_retry(f"/performers/{row['tpdb_performer_id']}", tok).get("data") or {}
                            g = norm_gender((p.get("extras") or {}).get("gender")); src = "tpdb"; time.sleep(tpdb_gap)
                    except Exception: GJ["errors"] += 1; time.sleep(5); continue
                    if not g: GJ["none"] += 1; continue
                    n = c.execute("UPDATE people SET gender=? WHERE id=? AND COALESCE(gender,'')='' AND COALESCE(edited_fields,'') NOT LIKE '%\"gender\"%'", (g, row["id"])).rowcount
                    c.commit()
                    if n: GJ["filled"] += 1; GJ[src] += 1
                db.set_setting(c, "gender_backfill_done", time.time()); c.commit()
            except Exception as ex: GJ["error"] = str(ex)[:200]
            finally:
                GJ.update(running=False, finished=time.time()); c.close()
        GJ.update(running=True, error=None); threading.Thread(target=work, daemon=True).start()
    if "pytest" not in __import__("sys").modules:
        _c = C()
        if not db.get_setting(_c, "gender_backfill_done", None): gender_job(wait=90)  # one-off, after start-up settles

    @app.get("/api/people/gender_job")
    def gender_job_status():
        c = C(); left = c.execute("SELECT COUNT(*) FROM people WHERE merged_into IS NULL AND COALESCE(gender,'')=''").fetchone()[0]
        return jsonify(dict(GJ, no_gender=left))

    @app.post("/api/people/gender_job")
    def gender_job_start():
        if not GJ.get("running"): gender_job()
        return jsonify(GJ)

    # ---------- career-info job: lightweight ThePornDB performer lookup for adult people never looked up ----------
    # Same lookup the person page does on first open (tpdb_update: hand-edited fields kept, manual ids never replaced,
    # Classic Porn people only get empty fields filled) plus the performer's scene count; no scene lists, no image download.
    CJ = dict(running=False, done=0, total=0, found=0, not_found=0, errors=0, started=None, finished=None, error=None)
    def career_job(delay=2.0, wait=60):
        def work():
            time.sleep(wait)
            import tpdb as tpdbmod
            c = C()
            try:
                tok = db.get_setting(c, "tpdb_key", "") or ""
                if not tok or db.get_setting(c, "career_job", "on") == "off": CJ.update(error="off" if tok else "No ThePornDB token set."); return
                rv = review_film_ids(c); e_of = lambda row: person_entry(c, row)["films"]
                linked = lambda r: r["tpdb_performer_id"] not in (None, TPDB_NONE)
                todo = [(r, k) for r, k in adult_people(c) if (r["tpdb_performer_id"] is None and r["source"] != "manual" and not r["tpdb_manual"])
                        or (linked(r) and r["tpdb_scenes_total"] is None and r["tpdb_scenes_json"] is None)]  # 2nd case: scene count only
                todo.sort(key=lambda rk: is_built(*rk))  # not-yet-opened people first
                CJ.update(total=len(todo), done=0, started=time.time())
                call = lambda path, **kw: tpdbmod.call_retry(path, tok, **kw)
                for row, classic in todo:
                    CJ["done"] += 1
                    try:
                        row = c.execute("SELECT * FROM people WHERE id=?", (row["id"],)).fetchone()
                        if not row or row["tpdb_scenes_json"] is not None: continue  # opened meanwhile
                        if linked(row):
                            p = dict(id=row["tpdb_performer_id"])  # already matched: only the scene count is missing
                            if row["tpdb_scenes_total"] is not None: continue
                        elif row["tpdb_performer_id"] is not None: continue
                        else: p = tpdb_resolve(c, tok, row, call)
                        if not p:
                            c.execute("UPDATE people SET tpdb_performer_id=?, tpdb_fetched_at=? WHERE id=? AND tpdb_performer_id IS NULL", (TPDB_NONE, time.time(), row["id"])); c.commit()
                            CJ["not_found"] += 1
                        else:
                            if not linked(row): apply_tpdb(c, row, p, allow_image=not (e_of(row) & rv), fill_only=classic)
                            meta = call(f"/performers/{p['id']}/scenes", per_page=1).get("meta")  # total only, no list
                            n = meta.get("total") if isinstance(meta, dict) else None
                            n = int(n) if isinstance(n, (int, str)) and str(n).isdigit() else None
                            if isinstance(n, int) and n > 0:
                                c.execute("UPDATE people SET tpdb_scenes_total=? WHERE id=? AND tpdb_scenes_json IS NULL", (n, row["id"])); c.commit()
                            CJ["found"] += 1
                    except urllib.error.HTTPError as ex:
                        CJ["errors"] += 1
                        if ex.code in (401, 403): CJ["error"] = f"ThePornDB refused ({ex.code})"; return
                        time.sleep(30)
                    except Exception: CJ["errors"] += 1; time.sleep(10)
                    try: c.commit()  # never keep a transaction open between people
                    except Exception: pass
                    time.sleep(delay)
            finally:
                CJ.update(running=False, finished=time.time()); c.close()
        CJ.update(running=True, error=None); threading.Thread(target=work, daemon=True).start()
    if "pytest" not in __import__("sys").modules: career_job()

    @app.get("/api/people/career_job")
    def career_job_status(): return jsonify(CJ)

    @app.post("/api/people/career_job")
    def career_job_start():
        if not CJ.get("running"): career_job(float((request.get_json(silent=True) or {}).get("delay", 2.0)), wait=0)
        return jsonify(CJ)
