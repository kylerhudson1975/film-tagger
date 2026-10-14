"""Person pages: library people index, TMDB person resolution/caching, filmography with owned-film matching."""
import os, re, json, time, threading, urllib.request, urllib.error
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

def init(app, tmdb, posters_dir):
    PHOTO_DIR = os.path.join(posters_dir, "people")
    fold = db.fold

    def C():
        c = db.connect(); c.executescript(PEOPLE_SCHEMA); return c

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
        return f"/api/person/{row['id']}/photo?v={int(row['fetched_at'] or 0)}" if row and row["profile_path"] else None

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
        rows = {r["name_norm"]: r for r in c.execute("SELECT * FROM people WHERE name_norm IN (%s)" % ",".join("?" * len(hits[:lim])), [k for k, _ in hits[:lim]])} if hits else {}
        out = []
        for k, e in hits[:lim]:
            r = rows.get(k); role = top_role(e)
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

    def fetch(c, key, row):
        p = tmdb(f"person/{row['tmdb_person_id']}", key, append_to_response="combined_credits", include_adult="true")
        cc = p.get("combined_credits") or {}
        keep = lambda x: x.get("media_type", "movie") == "movie"
        credits = dict(cast=[{k: x.get(k) for k in ("id", "title", "original_title", "release_date", "character", "poster_path")} for x in cc.get("cast", []) if keep(x)],
                       crew=[{k: x.get(k) for k in ("id", "title", "original_title", "release_date", "job", "department", "poster_path")} for x in cc.get("crew", []) if keep(x)])
        c.execute("UPDATE people SET profile_path=?, bio=?, birthday=?, deathday=?, place=?, known_for=?, credits_json=?, fetched_at=? WHERE id=?",
                  (p.get("profile_path"), p.get("biography"), p.get("birthday"), p.get("deathday"), p.get("place_of_birth"),
                   p.get("known_for_department"), json.dumps(credits), time.time(), row["id"]))
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
        key = key_of(c)
        if not key: status, msg = "no_key", "No TMDB API key set, so only the films in your library are shown."
        else:
            try:
                if row["tmdb_person_id"] is None or (request.args.get("refresh") and not row["tmdb_manual"] and row["tmdb_person_id"] == NO_MATCH):
                    tid = resolve(c, key, row, lib_ids)
                    c.execute("UPDATE people SET tmdb_person_id=?, fetched_at=NULL WHERE id=?", (tid, pid)); c.commit()
                    row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
                if row["tmdb_person_id"] and row["tmdb_person_id"] > 0 and (not row["credits_json"] or request.args.get("refresh")
                                                                              or time.time() - (row["fetched_at"] or 0) > STALE_S):
                    fetch(c, key, row); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
            except urllib.error.HTTPError as ex:
                status, msg = "offline", ("TMDB rejected the API key (401)." if ex.code == 401 else f"TMDB error {ex.code}.")
            except Exception as ex:
                status, msg = "offline", f"Could not reach TMDB ({type(ex).__name__}); showing cached/library data."
            if row["tmdb_person_id"] == NO_MATCH: status, msg = "no_match", "No matching person found on TMDB. You can set the TMDB person id manually."
        lib = [dict(r) for r in c.execute(f"SELECT id, title, year, poster, custom_poster, tmdb_id FROM films WHERE id IN ({','.join('?'*len(lib_ids))}) ORDER BY year IS NULL, year, title", lib_ids)] if lib_ids else []
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
        return jsonify(status=status, message=msg, person=dict(
            id=pid, name=row["name"], aka=[n for n in names if n != row["name"]], tmdb_person_id=tp, manual=bool(row["tmdb_manual"]),
            tmdb_url=tp and f"https://www.themoviedb.org/person/{tp}", photo=photo_url(row), bio=row["bio"], birthday=row["birthday"],
            deathday=row["deathday"], place=row["place"], fetched_at=row["fetched_at"],
            known_for=row["known_for"] or (ROLE_DEPT[top_role(e)] if e["roles"] else None)),
            roles={ro: len(s) for ro, s in e["roles"].items()}, library=lib, groups=groups, total=total, owned=owned)

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

    @app.get("/api/person/<int:pid>/photo")
    def person_photo(pid):
        """TMDB profile photo, cached locally under posters/people/ like posters."""
        c = C(); row = c.execute("SELECT tmdb_person_id, profile_path FROM people WHERE id=?", (pid,)).fetchone()
        if not row or not row["profile_path"]: return jsonify(error="No photo"), 404
        name = f"person-{row['tmdb_person_id']}-{os.path.basename(row['profile_path'])}"
        path = os.path.join(PHOTO_DIR, name)
        if not os.path.exists(path):
            try:
                with urllib.request.urlopen("https://image.tmdb.org/t/p/w300" + row["profile_path"], timeout=15) as r: data = r.read()
                os.makedirs(PHOTO_DIR, exist_ok=True)
                with open(path + ".tmp", "wb") as fh: fh.write(data)
                os.replace(path + ".tmp", path)
            except Exception as ex: return jsonify(error=f"Photo unavailable: {ex}"), 502
        return send_from_directory(PHOTO_DIR, name, max_age=86400 * 30)
