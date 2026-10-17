"""Person pages: library people index, TMDB person resolution/caching, filmography with owned-film matching."""
import os, re, json, time, threading, urllib.request, urllib.error, urllib.parse
from flask import request, jsonify, send_from_directory
import db

PEOPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY, tmdb_person_id INTEGER, name TEXT NOT NULL, name_norm TEXT UNIQUE,
  profile_path TEXT, bio TEXT, birthday TEXT, deathday TEXT, place TEXT, fetched_at REAL,
  known_for TEXT, credits_json TEXT, tmdb_manual INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_people_tmdb ON people(tmdb_person_id);
"""
ROLE_COLS = db.PEOPLE_ROLES  # role -> films column
ROLE_DEPT = {"cast": "Acting", "director": "Directing", "writer": "Writing", "producer": "Production",
             "cinematographer": "Camera", "composer": "Sound"}
DEPT_ORDER = ["Acting", "Directing", "Writing", "Production", "Camera", "Sound", "Editing", "Art", "Costume & Make-Up",
              "Visual Effects", "Crew", "Lighting"]
STALE_S = 14 * 86400
NO_MATCH = -1  # tmdb_person_id value meaning "searched TMDB, nothing found"
TPDB_NONE = "-"  # tpdb_performer_id value meaning "searched ThePornDB, nothing found"

# ThePornDB profile columns (adult performers). edited_fields = profile fields changed by hand; never overwritten.
TPDB_COLS = [("source", "TEXT"), ("tpdb_performer_id", "TEXT"), ("tpdb_manual", "INTEGER DEFAULT 0"), ("aliases", "TEXT"),
             ("nationality", "TEXT"), ("measurements", "TEXT"), ("height", "TEXT"), ("career", "TEXT"), ("links_json", "TEXT"),
             ("image_url", "TEXT"), ("gender", "TEXT"), ("edited_fields", "TEXT"), ("tpdb_fetched_at", "REAL"), ("photo_file", "TEXT"), ("tpdb_scenes_json", "TEXT"), ("tpdb_scenes_at", "REAL")]
EDITABLE = ("bio", "birthday", "place", "nationality", "measurements", "height", "career", "aliases", "links")
ADULT_HINT = re.compile(r"porn|xxx|adult", re.I)

def migrate(c):
    have = {r[1] for r in c.execute("PRAGMA table_info(people)")}
    for col, typ in TPDB_COLS:
        if col not in have: c.execute(f"ALTER TABLE people ADD COLUMN {col} {typ}")
    c.commit()

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

def apply_tpdb(c, row, p, allow_image=True):
    """Write a ThePornDB performer onto a people row, skipping hand-edited fields."""
    f = tpdb_fields(p); edited = set(json.loads(row["edited_fields"] or "[]"))
    if not allow_image: f["image_url"] = None
    sets = {k: v for k, v in f.items() if k not in edited}
    sets.update(source="tpdb", tpdb_performer_id=str(p.get("id")), tpdb_fetched_at=time.time(), fetched_at=time.time())
    c.execute("UPDATE people SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", list(sets.values()) + [row["id"]]); c.commit()

def tpdb_resolve(c, token, row, call):
    """Search /performers for the row's name; returns the full performer dict or None. `call(path, **params)`."""
    if row["tpdb_performer_id"] and row["tpdb_performer_id"] != TPDB_NONE:
        return call(f"/performers/{row['tpdb_performer_id']}").get("data") or None
    hit = pick_performer(call("/performers", q=row["name"]).get("data") or [], row["name"])
    if not hit: return None
    return call(f"/performers/{hit['id']}").get("data") or hit

def tpdb_update(c, token, row, call, allow_image=True):
    """Resolve + apply. Returns 'found' or 'not_found'. Manual ids are never replaced by search."""
    p = tpdb_resolve(c, token, row, call)
    if not p:
        if not row["tpdb_manual"]: c.execute("UPDATE people SET tpdb_performer_id=?, tpdb_fetched_at=? WHERE id=?", (TPDB_NONE, time.time(), row["id"])); c.commit()
        return "not_found"
    apply_tpdb(c, c.execute("SELECT * FROM people WHERE id=?", (row["id"],)).fetchone(), p, allow_image)
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

    def best_name(e): return max(e["names"].items(), key=lambda kv: (kv[1], len(kv[0])))[0]
    def top_role(e): return max(e["roles"].items(), key=lambda kv: len(kv[1]))[0]
    def photo_url(row):
        has = row and (row["photo_file"] or (row["image_url"] if row["source"] == "tpdb" else row["profile_path"]))
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
        rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM people WHERE name_norm IN (%s)" % ",".join("?" * len(hits[:lim])), [k for k, _ in hits[:lim]])} if hits else {}
        out = []
        for k, e in hits[:lim]:
            r = rows.get(k); role = top_role(e) if e["roles"] else "cast"
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

    def fetch(c, key, row, keep_profile=False):
        p = tmdb(f"person/{row['tmdb_person_id']}", key, append_to_response="combined_credits", include_adult="true")
        cc = p.get("combined_credits") or {}
        keep = lambda x: x.get("media_type", "movie") == "movie"
        credits = dict(cast=[{k: x.get(k) for k in ("id", "title", "original_title", "release_date", "character", "poster_path")} for x in cc.get("cast", []) if keep(x)],
                       crew=[{k: x.get(k) for k in ("id", "title", "original_title", "release_date", "job", "department", "poster_path")} for x in cc.get("crew", []) if keep(x)])
        edited = set(json.loads(row["edited_fields"] or "[]"))
        sets = dict(profile_path=p.get("profile_path"), bio=p.get("biography"), birthday=p.get("birthday"), deathday=p.get("deathday"),
                    place=p.get("place_of_birth"), known_for=p.get("known_for_department"), credits_json=json.dumps(credits),
                    fetched_at=time.time(), source="tmdb")
        if keep_profile:  # bio/photo/etc. come from ThePornDB or hand edits: only take the filmography
            sets = dict(credits_json=sets["credits_json"], known_for=sets["known_for"], fetched_at=sets["fetched_at"])
        sets = {k: v for k, v in sets.items() if k not in edited}
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
        e = index(c).get(row["name_norm"]) or {"names": {}, "roles": {}, "films": set()}
        lib_ids = sorted(e["films"])
        status, msg = "ok", None
        adult = bool(set(lib_ids) & adult_film_ids(c))
        mainstream = bool(set(lib_ids) - adult_film_ids(c))
        tp_note = None
        if adult:
            tok = db.get_setting(c, "tpdb_key", "") or ""
            if not tok: tp_note = "No ThePornDB token set."
            else:
                stale = time.time() - (row["tpdb_fetched_at"] or 0) > STALE_S
                if row["tpdb_performer_id"] is None or request.args.get("refresh") or (stale and row["tpdb_performer_id"] != TPDB_NONE):
                    import tpdb as tpdbmod
                    try: tpdb_update(c, tok, row, lambda path, **kw: tpdbmod.call_retry(path, tok, **kw), allow_image=not (set(lib_ids) & review_film_ids(c)))
                    except Exception as ex: tp_note = f"Could not reach ThePornDB ({type(ex).__name__})."
                    row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        on_tpdb = adult and row["tpdb_performer_id"] not in (None, TPDB_NONE) and row["source"] == "tpdb"
        key = key_of(c)
        manual = row["source"] == "manual" and not on_tpdb
        tm_err = None
        if key and not (row["tmdb_manual"] and row["tmdb_person_id"] == NO_MATCH):
            try:  # TMDB filmography for everyone with a TMDB person (profile fields stay from TPDB/hand edits)
                if row["tmdb_person_id"] is None or (request.args.get("refresh") and not row["tmdb_manual"] and row["tmdb_person_id"] == NO_MATCH):
                    tid = resolve(c, key, row, lib_ids)
                    c.execute("UPDATE people SET tmdb_person_id=? WHERE id=?", (tid, pid)); c.commit()
                    row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
                if row["tmdb_person_id"] and row["tmdb_person_id"] > 0 and (not row["credits_json"] or request.args.get("refresh")
                                                                              or time.time() - (row["fetched_at"] or 0) > STALE_S):
                    fetch(c, key, row, keep_profile=on_tpdb or manual); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
            except urllib.error.HTTPError as ex:
                tm_err = ("TMDB rejected the API key (401)." if ex.code == 401 else f"TMDB error {ex.code}.")
            except Exception as ex:
                tm_err = f"Could not reach TMDB ({type(ex).__name__}); showing cached/library data."
        on_tmdb = (row["tmdb_person_id"] or 0) > 0
        if on_tpdb: status, msg = "ok", tp_note
        elif manual: status, msg = "ok", None
        elif tm_err: status, msg = "offline", tm_err
        elif on_tmdb: status, msg = "ok", None
        elif adult and not mainstream: status, msg = "no_match", tp_note or "Not found on ThePornDB or TMDB. You can set either id manually."
        elif not key: status, msg = "no_key", "No TMDB API key set, so only the films in your library are shown."
        else: status, msg = "no_match", "No matching person found on TMDB. You can set the TMDB person id manually."
        scenes = None
        if on_tpdb:
            tok = db.get_setting(c, "tpdb_key", "") or ""
            if tok and (not row["tpdb_scenes_json"] or request.args.get("refresh") or time.time() - (row["tpdb_scenes_at"] or 0) > STALE_S):
                import tpdb as tpdbmod
                try:
                    allsc = []
                    for page in (1, 2, 3):
                        r = tpdbmod.call_retry(f"/performers/{row['tpdb_performer_id']}/scenes", tok, per_page=100, page=page)
                        d = r.get("data") or []; allsc += d
                        if len(d) < 100: break
                    keep = [dict(id=str(x.get("id")), _id=str(x.get("_id") or ""), title=x.get("title"), date=(x.get("date") or "")[:10],
                                 studio=(x.get("site") or {}).get("name"), slug=x.get("slug"),
                                 thumb=((x.get("posters") or {}).get("small") if isinstance(x.get("posters"), dict) else None) or x.get("poster") or x.get("image"))
                            for x in allsc]
                    c.execute("UPDATE people SET tpdb_scenes_json=?, tpdb_scenes_at=? WHERE id=?", (json.dumps(keep), time.time(), pid)); c.commit()
                    row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
                except Exception as ex: msg = (msg + " " if msg else "") + f"Could not load ThePornDB scenes ({type(ex).__name__})."
            if row["tpdb_scenes_json"]:
                img = not (set(lib_ids) & review_film_ids(c))
                owned = {}
                for r in c.execute("SELECT id, tpdb_id, poster, custom_poster FROM films WHERE tpdb_id IS NOT NULL AND tpdb_id<>''"):
                    owned[str(r["tpdb_id"])] = r
                scenes = []
                for x in json.loads(row["tpdb_scenes_json"]):
                    o = owned.get(x["id"]) or owned.get(x.get("_id") or "-")
                    scenes.append(dict(title=x["title"], date=x["date"], studio=x["studio"], url=f"https://theporndb.net/scenes/{x.get('slug') or x['id']}",
                                       film_id=o["id"] if o else None, thumb=(x["thumb"] if img else None),
                                       poster=((o["custom_poster"] or o["poster"]) if o and img else None)))
                scenes.sort(key=lambda z: z["date"] or "")
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
        source = ("ThePornDB" + (" + TMDB filmography" if groups else "")) if on_tpdb else ("Manual (added by hand)" + (" + TMDB filmography" if groups else "")) if manual else ("TMDB" if tp and row["credits_json"] else None)
        return jsonify(status=status, message=msg, person=dict(
            id=pid, name=row["name"], aka=[n for n in names if n != row["name"]], tmdb_person_id=tp, manual=bool(row["tmdb_manual"]),
            source=source, adult=adult, tpdb_performer_id=tpid, tpdb_manual=bool(row["tpdb_manual"]),
            tpdb_url=tpid and f"https://theporndb.net/performers/{tpid}", aliases=json.loads(row["aliases"] or "[]"),
            nationality=row["nationality"], measurements=row["measurements"], height=row["height"], career=row["career"],
            links=links, iafd=next((v for k, v in links.items() if k.lower() == "iafd"), None),
            edited=json.loads(row["edited_fields"] or "[]"),
            tmdb_url=tp and f"https://www.themoviedb.org/person/{tp}", photo=photo_url(row), bio=row["bio"], birthday=row["birthday"],
            deathday=row["deathday"], place=row["place"], fetched_at=row["fetched_at"],
            known_for=row["known_for"] or (ROLE_DEPT[top_role(e)] if e["roles"] else None)),
            roles={ro: len(s) for ro, s in e["roles"].items()}, library=lib, groups=groups, total=total, owned=owned, scenes=scenes)

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
            c.execute("UPDATE people SET tpdb_performer_id=NULL, tpdb_manual=0, tpdb_fetched_at=NULL WHERE id=?", (pid,)); c.commit(); return jsonify(ok=True)
        ident = parse_tpdb_performer(v)
        if not ident: return jsonify(error=f"Not a ThePornDB performer id or URL: {v}"), 400
        tok = db.get_setting(c, "tpdb_key", "") or ""
        if not tok: return jsonify(error="No ThePornDB token set (Settings)."), 400
        import tpdb as tpdbmod
        try: p = tpdbmod.call_retry(f"/performers/{ident}", tok).get("data")
        except urllib.error.HTTPError as ex: return jsonify(error=f"ThePornDB error {ex.code}"), 502
        except Exception as ex: return jsonify(error=f"Could not reach ThePornDB ({type(ex).__name__})"), 502
        if not p: return jsonify(error="Performer not found on ThePornDB"), 404
        c.execute("UPDATE people SET tpdb_manual=1 WHERE id=?", (pid,))
        e = index(c).get(row["name_norm"]) or {"films": set()}
        apply_tpdb(c, c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone(), p, allow_image=not (e["films"] & review_film_ids(c)))
        return jsonify(ok=True, name=p.get("name"))

    def save_photo(c, pid, data, ext):
        ext = ext.lower() if ext and ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"
        os.makedirs(PHOTO_DIR, exist_ok=True)
        name = f"manual-{pid}-{int(time.time())}{ext}"
        with open(os.path.join(PHOTO_DIR, name), "wb") as fh: fh.write(data)
        ed = set(json.loads(c.execute("SELECT edited_fields FROM people WHERE id=?", (pid,)).fetchone()[0] or "[]")) | {"photo"}
        c.execute("UPDATE people SET photo_file=?, edited_fields=?, fetched_at=? WHERE id=?", (name, json.dumps(sorted(ed)), time.time(), pid)); c.commit()

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
            if v != row[col]: c.execute(f"UPDATE people SET {col}=? WHERE id=?", (v, pid)); edited.add(col)
        c.execute("UPDATE people SET edited_fields=? WHERE id=?", (json.dumps(sorted(edited)), pid)); c.commit()
        return jsonify(ok=True, edited=sorted(edited))

    @app.get("/api/person/<int:pid>/photo")
    def person_photo(pid):
        """Profile photo (TMDB or ThePornDB), cached locally under posters/people/ like posters."""
        c = C(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if row and row["photo_file"] and os.path.exists(os.path.join(PHOTO_DIR, row["photo_file"])):
            return send_from_directory(PHOTO_DIR, row["photo_file"], max_age=86400 * 30)
        tp = row and row["source"] == "tpdb" and row["image_url"]
        if not row or not (tp or row["profile_path"]): return jsonify(error="No photo"), 404
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
