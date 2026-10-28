"""Person pages: library people index, TMDB person resolution/caching, filmography with owned-film matching."""
import os, re, json, time, threading, urllib.request, urllib.error, urllib.parse
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
TPDB_MAX_PAGES = 200  # 100 scenes per page (20,000 scenes)
TPDB_NONE = "-"  # tpdb_performer_id value meaning "searched ThePornDB, nothing found"

# ThePornDB profile columns (adult performers). edited_fields = profile fields changed by hand; never overwritten.
TPDB_COLS = [("source", "TEXT"), ("tpdb_performer_id", "TEXT"), ("tpdb_manual", "INTEGER DEFAULT 0"), ("aliases", "TEXT"),
             ("nationality", "TEXT"), ("measurements", "TEXT"), ("height", "TEXT"), ("career", "TEXT"), ("links_json", "TEXT"),
             ("image_url", "TEXT"), ("gender", "TEXT"), ("edited_fields", "TEXT"), ("tpdb_fetched_at", "REAL"), ("photo_file", "TEXT"), ("tpdb_scenes_json", "TEXT"), ("tpdb_scenes_at", "REAL"),
             ("tpdb_scenes_total", "INTEGER"), ("merged_into", "INTEGER"), ("photo_orig", "TEXT"), ("birth_date_source", "TEXT")]
EDITABLE = ("bio", "birthday", "place", "nationality", "measurements", "height", "career", "aliases", "links")
ADULT_HINT = re.compile(r"porn|xxx|adult", re.I)
COCREDIT = {}  # "ids": fn(conn, [person ids]) -> library film ids credited to all of them (app.build_query cocredit=)
RLBL_CO = {"director": "Director", "cast": "Actor", "writer": "Writer", "producer": "Producer", "cinematographer": "Cinematographer", "composer": "Composer"}

def migrate(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(people)")}
    for col, typ in TPDB_COLS:
        if col not in have: c.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")
    c.execute(REVIEW_SCHEMA)
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
    return (str(v).strip() or None) if v is not None else None

def adult_roots(c):
    """Scene libraries plus adult film libraries (setting adult_roots, else library folders named porn/xxx/adult)."""
    extra = db.get_setting(c, "adult_roots", None)
    if extra is None: extra = [r for r in (db.get_setting(c, "roots", []) or []) if r and ADULT_HINT.search(os.path.basename(r.rstrip("\\/")) or r)]
    return [r.rstrip("\\/") for r in list(db.scene_roots(c)) + list(extra or []) if r]

def adult_film_ids(c):
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
                gender=ex.get("gender") or None)

def apply_tpdb(c, row, p, allow_image=True, fill_only=False):
    """Write a ThePornDB performer onto a people row, skipping hand-edited fields. fill_only (TMDB-primary people, e.g.
    anyone in Classic Porn): only fill fields that are empty and keep the TMDB source; the ThePornDB image is stored as a
    fallback photo (used only when TMDB has none)."""
    f = tpdb_fields(p); edited = set(json.loads(row["edited_fields"] or "[]"))
    if not allow_image: f["image_url"] = None
    sets = {k: v for k, v in f.items() if k not in edited}
    if fill_only: sets = {k: v for k, v in sets.items() if v and (k == "image_url" or not row[k])}
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

def init(app, tmdb, posters_dir):
    PHOTO_DIR = os.path.join(posters_dir, "people")
    fold = db.fold

    def C():
        c = db.connect(); c.executescript(PEOPLE_SCHEMA); migrate(c); return c

    # ---------- library people index (fold(name) -> names, roles, film ids) ----------
    IDX = {"at": 0, "key": None, "data": {}}
    IDX_LOCK = threading.Lock()
    def index(c):
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
                                     studio=(x.get("site") or {}).get("name"), slug=x.get("slug"),
                                     thumb=((x.get("posters") or {}).get("small") if isinstance(x.get("posters"), dict) else None) or x.get("poster") or x.get("image"))
                                for x in allsc]
                        c2.execute("UPDATE people SET tpdb_scenes_json=?, tpdb_scenes_at=?, tpdb_scenes_total=? WHERE id=?",
                                   (json.dumps(keep), time.time() if final else 0, st["total"] or len(keep), pid)); c2.commit()
                    if final: break
                    page += 1
            except Exception as ex: st["error"] = type(ex).__name__
            finally: st["running"] = False; c2.close()
        threading.Thread(target=run, daemon=True).start()

    def best_name(e): return max(e["names"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    def top_role(e): return max(e["roles"].items(), key=lambda kv: len(kv[1]))[0]
    def photo_url(row):
        has = row and (row["photo_file"] or photo_choice(row))
        return f"/api/person/{row['id']}/photo?v={int(row['fetched_at'] or 0)}" if has else None

    def ensure_row(c, name):
        k = fold(" ".join(name.split()))
        r = c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()
        if r: return r
        c.execute("INSERT INTO people(name, name_norm) VALUES(?,?)", (" ".join(name.split()), k)); c.commit()
        return c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()

    @app.get("/api/people/search")
    def people_search():
        """Accent/case-insensitive name search over everyone credited in the library."""
        q = fold(request.args.get("q") or "").split()
        if not q: return jsonify(people=[], total=0)
        lim = min(int(request.args.get("limit", 24)), 200)
        c = C(); data = index(c)
        hits = [(k, e) for k, e in data.items() if all(w in k for w in q)]
        def score(ke):
            k, e = ke; toks = k.split()
            return (0 if k == " ".join(q) else 1 if all(any(t.startswith(w) for t in toks) for w in q) else 2, -len(e["films"]), k)
        hits.sort(key=score)
        have = {k for k, _ in hits}
        for r in c.execute("SELECT name, name_norm FROM people WHERE source='manual'"):
            if r["name_norm"] not in have and all(w in r["name_norm"] for w in q):
                hits.append((r["name_norm"], {"names": {r["name"]: 1}, "roles": {}, "films": set()}))
        for k2, names in db.alias_groups(c).items():  # a search for any alias finds the person
            if all(w in k2 for w in q):
                for n in names:
                    kn = fold(n)
                    if kn not in have and kn in data: hits.append((kn, data[kn])); have.add(kn)
        rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM people WHERE name_norm IN (%s)" % ",".join("?" * len(hits[:lim])), [k for k, _ in hits[:lim]])} if hits else {}
        out = []; seen_pid = set()
        for k, e in hits[:lim]:
            r = rows.get(k); role = top_role(e) if e["roles"] else "cast"
            if r and r["merged_into"]:
                t = c.execute("SELECT * FROM people WHERE id=?", (r["merged_into"],)).fetchone()
                if t: r = t; k = t["name_norm"]; e = dict(person_entry(c, t), names={t["name"]: 1})
            if r and r["id"] in seen_pid: continue
            if r: seen_pid.add(r["id"])
            out.append(dict(name=best_name(e), key=k, pid=r["id"] if r else None, n=len(e["films"]), role=role,
                            known_for=(r["known_for"] if r and r["known_for"] else ROLE_DEPT[role]),
                            roles={ro: len(s) for ro, s in e["roles"].items()}, photo=photo_url(r)))
        return jsonify(people=out, total=len(hits))

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
        tids = [r[0] for r in c.execute(f"SELECT tmdb_id FROM films WHERE tmdb_id IS NOT NULL AND id IN ({','.join('?'*len(lib_ids))}) ORDER BY year DESC LIMIT 4", list(lib_ids))] if lib_ids else []
        for tid in tids:
            try: cr = tmdb(f"movie/{tid}/credits", key)
            except urllib.error.HTTPError as e:
                if e.code == 401: raise
                continue
            for p in cr.get("cast", []) + cr.get("crew", []):
                if fold(p.get("name") or "") == target or fold(p.get("original_name") or "") == target: return p["id"]
        res = tmdb("search/person", key, query=row["name"], include_adult="true").get("results", [])
        exact = [p for p in res if fold(p.get("name") or "") == target or fold(p.get("original_name") or "") == target]
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
        q = lambda s_: [r[0] for r in c.execute(f"SELECT tmdb_id FROM films WHERE tmdb_id IS NOT NULL AND id IN ({','.join('?'*len(s_))}) ORDER BY year DESC", list(s_))] if s_ else []
        ctids = q(cl); ltids = set(q(set(ids)))
        hit = {}
        for tid in ctids[:6]:
            try: cr = tmdb(f"movie/{tid}/credits", key)
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
        keep = lambda x: x.get("media_type", "movie") == "movie"
        credits = dict(cast=[{k: x.get(k) for k in ("id", "title", "original_title", "release_date", "character", "poster_path")} for x in cc.get("cast", []) if keep(x)],
                       crew=[{k: x.get(k) for k in ("id", "title", "original_title", "release_date", "job", "department", "poster_path")} for x in cc.get("crew", []) if keep(x)])
        edited = set(json.loads(row["edited_fields"] or "[]"))
        sets = dict(profile_path=p.get("profile_path"), bio=p.get("biography"), birthday=p.get("birthday"), deathday=p.get("deathday"),
                    place=p.get("place_of_birth"), known_for=p.get("known_for_department"), credits_json=json.dumps(credits),
                    fetched_at=time.time(), source="tmdb")
        if keep_profile:  # bio/photo/etc. come from ThePornDB or hand edits: only take the filmography (and a birth date ThePornDB lacks)
            sets = dict(credits_json=sets["credits_json"], known_for=sets["known_for"], fetched_at=sets["fetched_at"],
                        **({"birthday": sets["birthday"]} if sets["birthday"] and not row["birthday"] else {}))
        if tmdb_primary:  # TMDB is the primary profile; fields TMDB lacks keep what ThePornDB / others filled in
            sets = {k: v for k, v in sets.items() if v not in (None, "")}; sets["source"] = "tmdb"
        if not sets.get("birthday"): sets.pop("birthday", None)  # TMDB has none: keep one found elsewhere
        elif sets["birthday"] != row["birthday"]: sets["birth_date_source"] = f"tmdb person {row['tmdb_person_id']}"
        sets = {k: v for k, v in sets.items() if k not in edited and not (k == "birth_date_source" and "birthday" in edited)}
        c.execute("UPDATE people SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", list(sets.values()) + [row["id"]])
        c.commit()

    def filmography(c, credits):
        lib = [dict(r) for r in c.execute("SELECT id, title, original_title, year, tmdb_id, poster, custom_poster FROM films")]
        by_tmdb = {f["tmdb_id"]: f for f in lib if f["tmdb_id"]}
        by_ty = {}
        for f in lib:
            for t in (f["title"], f["original_title"]):
                if t and f["year"]: by_ty.setdefault((fold(t), f["year"]), f)
        groups = {}
        def add(dept, x, what):
            yr = int(x["release_date"][:4]) if (x.get("release_date") or "")[:4].isdigit() else None
            g = groups.setdefault(dept, {})
            e = g.get(x["id"])
            if e:
                if what and what not in e["role"].split(" / "): e["role"] += " / " + what
                return
            own = by_tmdb.get(x["id"]) or (yr and (by_ty.get((fold(x.get("title") or ""), yr)) or by_ty.get((fold(x.get("original_title") or ""), yr))))
            g[x["id"]] = dict(tmdb_id=x["id"], title=x.get("title") or x.get("original_title"), year=yr, date=x.get("release_date") or "",
                              role=what or "", thumb=x.get("poster_path") and "https://image.tmdb.org/t/p/w92" + x["poster_path"],
                              film_id=own["id"] if own else None,
                              poster=(own["custom_poster"] or own["poster"]) if own else None)
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
        if groups:
            allm = {}
            for g in groups:
                for x in g["credits"]: allm[x["tmdb_id"]] = x["film_id"]
            total, owned = len(allm), sum(1 for v in allm.values() if v)
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
            nationality=row["nationality"], measurements=row["measurements"], height=row["height"], height_display=fmt_height(row["height"], height_units(c)), career=row["career"],
            links=links, iafd=next((v for k, v in links.items() if k.lower() == "iafd"), None),
            edited=json.loads(row["edited_fields"] or "[]"), photo_cropped=bool(row["photo_orig"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_orig"]))),
            merged=[dict(id=r2["id"], name=r2["name"]) for r2 in c.execute("SELECT id, name FROM people WHERE merged_into=? ORDER BY name", (pid,))],
            tmdb_url=tp and f"https://www.themoviedb.org/person/{tp}", photo=photo_url(row), bio=row["bio"], birthday=row["birthday"], birth_date_source=row["birth_date_source"],
            deathday=row["deathday"], place=row["place"], fetched_at=row["fetched_at"],
            known_for=row["known_for"] or (ROLE_DEPT[top_role(e)] if e["roles"] else None)),
            roles={ro: len(s) for ro, s in e["roles"].items()}, library=lib, groups=groups, total=total, owned=owned, scenes=scenes,
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

    def save_photo(c, pid, data, ext):
        ext = ext.lower() if ext and ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
        os.makedirs(PHOTO_DIR, exist_ok=True)
        name = f"manual-{pid}-{int(time.time())}{ext}"
        with open(os.path.join(PHOTO_DIR, name), "wb") as fh: fh.write(data)
        ed = set(json.loads(c.execute("SELECT edited_fields FROM people WHERE id=?", (pid,)).fetchone()[0] or "[]")) | {"photo"}
        c.execute("UPDATE people SET photo_file=?, photo_orig=NULL, edited_fields=?, fetched_at=? WHERE id=?", (name, json.dumps(sorted(ed)), time.time(), pid)); c.commit()

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
        return jsonify(ok=True, id=r["id"], name=r["name"], existed=bool(r["source"] != "manual" or r["fetched_at"] is None), warning=err)

    @app.post("/api/person/<int:pid>/photo/upload")
    def person_photo_upload(pid):
        c = C()
        if not c.execute("SELECT 1 FROM people WHERE id=?", (pid,)).fetchone(): return jsonify(error="Unknown person"), 404
        err = photo_from_request(c, pid)
        return (jsonify(error=err), 502) if err else jsonify(ok=True)

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
        if old and old not in (name, orig) and ".crop-" in old:
            try: os.remove(os.path.join(PHOTO_DIR, old))
            except OSError: pass
        return jsonify(ok=True, width=cw, height=ch, orig_width=w, orig_height=h, photo=f"/api/person/{pid}/photo?v={int(time.time())}")

    @app.post("/api/person/<int:pid>/photo/reset_crop")
    def person_photo_reset_crop(pid):
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        if not (row["photo_orig"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_orig"]))): return jsonify(error="The photo is not cropped"), 400
        old = row["photo_file"]
        c.execute("UPDATE people SET photo_file=?, photo_orig=NULL, fetched_at=? WHERE id=?", (row["photo_orig"], time.time(), pid)); c.commit()
        if old and ".crop-" in old:
            try: os.remove(os.path.join(PHOTO_DIR, old))
            except OSError: pass
        return jsonify(ok=True)

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
        c.execute("UPDATE people SET aliases=?, edited_fields=? WHERE id=?", (json.dumps(als), json.dumps(sorted(edited)), pid))
        c.execute("UPDATE people SET merged_into=? WHERE id=?", (pid, o["id"]))
        c.execute("UPDATE people SET merged_into=? WHERE merged_into=?", (pid, o["id"]))  # people merged into the other move along
        c.commit()
        return jsonify(ok=True, id=pid, merged=o["name"], aliases=als)

    @app.post("/api/person/<int:pid>/unmerge")
    def person_unmerge(pid):
        """Undo a merge for one person row that was merged into another (its name stays as an alias until removed by hand)."""
        c = C(); c.execute("UPDATE people SET merged_into=NULL WHERE id=?", (pid,)); c.commit(); return jsonify(ok=True)

    @app.post("/api/person/<int:pid>/edit")
    def person_edit(pid):
        """Hand edits to profile fields; edited fields are never overwritten by TPDB/TMDB fetches."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        d = request.json or {}; edited = set(json.loads(row["edited_fields"] or "[]"))
        for k in EDITABLE:
            if k not in d: continue
            v = d[k]
            col = "links_json" if k == "links" else k
            v = clean_value(k, v)
            if v != row[col]:
                c.execute(f"UPDATE people SET {col}=? WHERE id=?", (v, pid)); edited.add(col)
                if col == "birthday": c.execute("UPDATE people SET birth_date_source=? WHERE id=?", ("hand edit" if v else None, pid))
        c.execute("UPDATE people SET edited_fields=? WHERE id=?", (json.dumps(sorted(edited)), pid)); c.commit()
        return jsonify(ok=True, edited=sorted(edited))

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
                         tmdb_id=it0.get("tmdb_id"), url=it0.get("url") or (f"https://www.themoviedb.org/movie/{it0.get('tmdb_id')}" if it0.get("tmdb_id") else None),
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

    def co_suggest(c, row, qtext, lim, extra=4, roles_of=None):
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
        out = [x for x in out if x["total"] or q][:lim]
        for x in out: x.pop("_sc")
        return out

    # ---------- Person page insights (Dashboard-style summary above the filmography) ----------
    INS = {}  # (person id, data key) -> (at, result)
    def co_dkey(c):
        return (tuple(c.execute("SELECT COUNT(*), MAX(updated_at), SUM(COALESCE(rating,0)), SUM(COALESCE(watched,0)) FROM films").fetchone()),
                tuple(c.execute("SELECT COUNT(*), MAX(COALESCE(fetched_at,0)), MAX(COALESCE(tpdb_scenes_at,0)), SUM(LENGTH(COALESCE(birthday,''))), MAX(id) FROM people").fetchone()))
    def person_rows(c, row, dk=None):
        """One person's credits (library + cached TMDB/ThePornDB filmography) as rows. Adult titles where they performed
        only count with a known birth date and 18+ at release (same rule as Reports / Credited with)."""
        dk = dk or co_dkey(c); e = person_entry(c, row)
        ik = (row["id"], dk[0], dk[1]); its = CO_ITEMS.get(ik)
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
                         genre="", country="", rating=None, in_library=False, adult=adult, tmdb_id=it.get("tmdb_id") if k[0] == "T" else None)
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
    SJOB = {}   # canonical person id -> progress of the on-demand job
    SALL = {}   # (person id, data key, cached casts) -> result

    def stable_all_films(c, row):
        """(films for stable_tiers incl. not-in-library TMDB titles with a cached cast, TMDB ids still missing a cast, e)."""
        rows, _, e, F, xrows = person_rows(c, row)
        need = sorted({r["tmdb_id"] for r in rows + xrows if not r["in_library"] and r.get("tmdb_id") and stable_want(r["roles"], "all")})
        cache = {}
        for i in range(0, len(need), 500):
            ch = need[i:i + 500]
            for t, cj in c.execute(f"SELECT tmdb_id, cast_json FROM tmdb_movie_cast WHERE tmdb_id IN ({','.join('?' * len(ch))})", ch):
                try: cache[t] = json.loads(cj or "[]")
                except Exception: cache[t] = []
        films = []
        for r in rows + [x for x in xrows]:
            if r["in_library"]: films.append(dict(r, cast=(F[r["film_id"]]["cast_"] or "").split(",")))
            elif r.get("tmdb_id") in cache: films.append(dict(r, cast=[x[0] for x in cache[r["tmdb_id"]]], tmdb_cast=cache[r["tmdb_id"]]))
        return films, [t for t in need if t not in cache], e

    def stable_all_job(pid):
        st = SJOB[pid] = dict(running=True, phase="filmography", done=0, total=0, errors=0, error=None, started=time.time())
        def work():
            c = C()
            try:
                c.execute(CAST_SCHEMA)
                row = co_canon(c, pid); key = key_of(c)
                if not key: st["error"] = "No TMDB key is set, so only library titles can be counted."; return
                if not row["credits_json"] and (row["tmdb_person_id"] or 0) > 0:
                    fetch(c, key, row, keep_profile=(row["source"] or "") in ("tpdb", "manual")); CO_ITEMS.clear(); row = co_canon(c, pid)
                films, missing, e = stable_all_films(c, row)
                st.update(phase="casts", total=len(missing), done=0)
                for t in missing:
                    time.sleep(0.25)  # ~4 TMDB requests a second
                    try: m = tmdb(f"movie/{t}/credits", key)
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
                    c.execute("INSERT OR REPLACE INTO tmdb_movie_cast(tmdb_id, cast_json, at) VALUES(?,?,?)", (t, json.dumps(cast), time.time())); c.commit()
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
        c = C(); c.execute(CAST_SCHEMA); row = co_canon(c, pid)
        if not row: return jsonify(error="Unknown person"), 404
        st = SJOB.get(row["id"])
        if st and st.get("running"):
            return jsonify(status="loading", phase=st["phase"], done=st["done"], total=st["total"])
        films, missing, e = stable_all_films(c, row)
        fresh = st and time.time() - st.get("finished", 0) < 600
        if not fresh and (missing or (not row["credits_json"] and (row["tmdb_person_id"] or 0) > 0) or request.args.get("refresh")):
            st = stable_all_job(row["id"])
            return jsonify(status="loading", phase=st["phase"], done=0, total=0)
        dk = co_dkey(c); n_cast = c.execute("SELECT COUNT(*) FROM tmdb_movie_cast").fetchone()[0]
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
        dk = co_dkey(c); ck = (row["id"], dk)
        hit = INS.get(ck)
        if hit and time.time() - hit[0] < 600 and not request.args.get("force"): return jsonify(dict(hit[1], cached=True))
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
        costars = co_suggest(c, row, "", 8, extra=4, roles_of="others")
        directors = [x for x in co_suggest(c, row, "", 5, extra=3, roles_of="director") if x["total"]]
        # Stable / Muses / Harem: performers in films they directed (or directed/wrote/produced), library titles only
        stable = None
        if e["roles"].get("director"):
            dlib = lib + [r for r in xrows if r["in_library"]]  # a director who also appears in their own film still directed it
            films = [dict(r, cast=(F[r["film_id"]]["cast_"] or "").split(",")) for r in dlib]
            stable = stable_tiers(c, row, e, films)
        res = dict(id=row["id"], name=row["name"], total=len(rows), in_library=len(lib), not_in_library=len(rows) - len(lib), excluded=excluded,
                   adult=any(r["adult"] for r in rows) or bool(excluded), birthday=row["birthday"],
                   career=[min(yrs), max(yrs)] if yrs else None, busiest=dict(year=busy[0], n=busy[1], lib=yl[busy[0]]) if busy else None,
                   years=[dict(k=y, lib=yl[y], other=yrs[y] - yl[y]) for y in sorted(yrs)], undated=sum(1 for r in rows if not r["year"]),
                   top_genre=top(gen, 1)[0] if gen else None, top_role=top(rol, 1)[0] if rol else None, roles=top(rol, 8),
                   rating=dict(avg=round(sum(rated) / len(rated), 2), n=len(rated)) if rated else None,
                   genres=top(gen), studios=top(stu), countries=top(cty), costars=costars, directors=directors, stable=stable)
        INS[ck] = (time.time(), res)
        if len(INS) > 128: INS.pop(next(iter(INS)))
        return jsonify(dict(res, cached=False))
