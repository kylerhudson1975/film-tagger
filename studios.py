"""Studio pages and the Studios list.

Library side: every count comes from reports.studio_titles / app.build_query(studio=...), the same matching as the
studio= library filter (each comma-separated studio entry, hidden aliases under the main name, scene <Studio> folders),
so a studio's 'in library' number always equals its library list.

Online side (optional, cached in the DB, fails gracefully offline):
  TMDB company (found through a library film's production_companies, else an exact-name company search): logo,
  origin country, headquarters, parent company, description, homepage, and the catalog size via discover/movie
  ?with_companies= (adult titles included). Scene studios: the ThePornDB site (logo, parent/network, description) and its
  scene count. A background job fills this for every studio in the library (rate-limited, resumable).
Country: hand edit > TMDB origin country > the most common production country of the studio's library titles
(marked 'inferred'). Hand edits (country, TMDB company id) are never overwritten."""
import json, re, threading, time, urllib.error
from collections import Counter
import db
import reports as repmod
import tpdb as tpdbmod

SCHEMA = """
CREATE TABLE IF NOT EXISTS studio_info(
  key TEXT PRIMARY KEY, name TEXT,
  tmdb_company_id INTEGER, tmdb_match TEXT, tmdb_checked REAL, tmdb_error TEXT, tmdb_name TEXT,
  logo TEXT, origin_country TEXT, headquarters TEXT, parent TEXT, description TEXT, homepage TEXT, tmdb_total INTEGER,
  tpdb_site_id TEXT, tpdb_checked REAL, tpdb_error TEXT, tpdb_name TEXT, tpdb_url TEXT, tpdb_logo TEXT, tpdb_desc TEXT,
  tpdb_parent TEXT, tpdb_total INTEGER,
  country TEXT, edited_fields TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS studio_catalog(key TEXT PRIMARY KEY, src TEXT, items_json TEXT, total INTEGER, capped INTEGER, fetched_at REAL);
"""
TMDB_IMG = "https://image.tmdb.org/t/p/w185"
TMDB_PAGES = 50       # discover: 20 per page -> the first 1,000 titles of a catalog are listed (the total is always exact)
TPDB_PAGES = 10       # ThePornDB: 100 per page
CATALOG_DAYS = 30
TMDB_GAP = 0.3        # seconds between TMDB requests (well under TMDB's limit)
# Offline fallback for country names / flags (TMDB's configuration/countries list replaces it once fetched).
FALLBACK_COUNTRIES = {
    "US": "United States of America", "GB": "United Kingdom", "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "NL": "Netherlands", "BE": "Belgium", "CH": "Switzerland",
    "AT": "Austria", "CA": "Canada", "AU": "Australia", "JP": "Japan", "HK": "Hong Kong", "CN": "China", "KR": "South Korea",
    "IN": "India", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina", "CZ": "Czech Republic", "HU": "Hungary", "PL": "Poland",
    "RU": "Russia", "IE": "Ireland", "NZ": "New Zealand", "PT": "Portugal", "GR": "Greece", "IS": "Iceland", "RO": "Romania",
    "ZA": "South Africa", "TR": "Turkey", "IL": "Israel", "PH": "Philippines", "TH": "Thailand", "TW": "Taiwan",
    "XC": "Czechoslovakia", "XG": "East Germany", "SU": "Soviet Union", "YU": "Yugoslavia", "LU": "Luxembourg", "MC": "Monaco"}

JOB = dict(running=False, stop=False, done=0, total=0, tmdb=0, tpdb=0, none=0, errors=0, current=None,
           started_at=None, finished_at=None, last_error=None)
TMDB = {"fn": None}   # app.tmdb (replaceable in tests)
PAGEJOBS = {}   # studio key -> {running, phase, done, total, error, finished}
LOCK = threading.Lock()


def ensure(c):
    c.executescript(SCHEMA)


def enabled():
    """Off for throwaway databases in the temp folder (tests) so they never go online on their own."""
    import os, tempfile
    return not os.path.abspath(db.DB_PATH).startswith(os.path.abspath(tempfile.gettempdir()))


def country_names(c):
    m = db.get_setting(c, "tmdb_countries", None)
    return dict(FALLBACK_COUNTRIES, **m) if isinstance(m, dict) else dict(FALLBACK_COUNTRIES)


def iso_of(name, cmap):
    if not name: return None
    n = db.fold(name)
    for k, v in cmap.items():
        if db.fold(v) == n: return k
    return None


def main_key(c, name):
    """studio_key of the main name for any spelling or hidden alias."""
    k = db.studio_key(name)
    amap = db.studio_alias_map(c)
    return db.studio_key(amap[k]) if k in amap else k


def _edited(row):
    try: return set(json.loads((row["edited_fields"] if row else None) or "[]"))
    except Exception: return set()


def inferred_country(cty_of, fids):
    """Most common production country over the given titles: (name, titles with it, titles with any country)."""
    cnt, n = Counter(), 0
    for fid in fids:
        cs = [x.strip() for x in (cty_of.get(fid) or "").split(",") if x.strip()]
        if cs: n += 1
        for x in dict.fromkeys(cs): cnt[x] += 1
    if not cnt: return None
    name, k = sorted(cnt.items(), key=lambda kv: (-kv[1], db.fold(kv[0])))[0]
    return dict(name=name, n=k, of=n)


def country_of(row, inferred, cmap):
    """(country name, ISO code, source) with source 'hand' | 'tmdb' | 'inferred' | None."""
    if row is not None and "country" in _edited(row) and (row["country"] or "").strip():
        nm = row["country"].strip(); return nm, iso_of(nm, cmap), "hand"
    if row is not None and (row["origin_country"] or "").strip():
        iso = row["origin_country"].strip().upper(); return cmap.get(iso, iso), iso, "tmdb"
    if inferred: return inferred["name"], iso_of(inferred["name"], cmap), "inferred"
    return None, None, None


def init(app, tmdb, build_query):
    from flask import request, jsonify

    TMDB["fn"] = tmdb
    def C():
        c = db.connect(); ensure(c); return c
    c0 = C(); c0.close()

    def tm(path, key, **kw):
        time.sleep(TMDB_GAP)
        return TMDB["fn"](path, key, **kw)

    def keys_of(c):
        return (db.get_setting(c, "tmdb_key", "") or "").strip(), (db.get_setting(c, "tpdb_key", "") or "").strip()

    def load_countries(c, key):
        if db.get_setting(c, "tmdb_countries", None) or not key: return
        try:
            l = tm("configuration/countries", key)
            m = {x["iso_3166_1"]: x.get("english_name") or x.get("native_name") for x in l if x.get("iso_3166_1")}
            if m: db.set_setting(c, "tmdb_countries", m); c.commit()
        except Exception: pass

    def whole(c):
        return repmod.studio_titles(c, "SELECT id FROM films")

    def upsert(c, key, name, **f):
        c.execute("INSERT OR IGNORE INTO studio_info(key, name) VALUES(?,?)", (key, name))
        f["updated_at"] = time.time(); f["name"] = name
        c.execute(f"UPDATE studio_info SET {', '.join(k + '=?' for k in f)} WHERE key=?", list(f.values()) + [key])

    def row_of(c, key):
        return c.execute("SELECT * FROM studio_info WHERE key=?", (key,)).fetchone()

    # ---------- TMDB company ----------
    def lookup_tmdb(c, key, name, fids, api_key, scene_only=False):
        """Find and store the TMDB company. Returns 'tmdb' (found), 'none' (looked, nothing) ; raises on network errors."""
        row = row_of(c, key); ed = _edited(row)
        names = db.studio_names(name, c); ks = {db.studio_key(n) for n in names} | {key}
        cid, how = None, None
        if row is not None and "tmdb" in ed and row["tmdb_company_id"]:
            cid, how = row["tmdb_company_id"], "manual"
        else:
            tids = [r[0] for r in c.execute(f"SELECT tmdb_id FROM films WHERE tmdb_id IS NOT NULL AND id IN ({','.join(map(str, fids)) or 'NULL'}) "
                                            "ORDER BY year IS NULL, year LIMIT 3")]
            cnt = Counter()
            for t in tids:
                try: m = tm(f"movie/{t}", api_key)
                except urllib.error.HTTPError as ex:
                    if ex.code == 404: continue
                    raise
                for pc in m.get("production_companies") or []:
                    if pc.get("id") and db.studio_key(pc.get("name")) in ks: cnt[pc["id"]] += 1
                if cnt: break
            if cnt: cid, how = cnt.most_common(1)[0][0], "film"
            elif not scene_only:
                res = (tm("search/company", api_key, query=name) or {}).get("results") or []
                exact = [x for x in res if db.studio_key(x.get("name")) in ks]
                if exact:
                    exact.sort(key=lambda x: not (x.get("origin_country") or ""))  # TMDB's order, ones with a country first
                    cid, how = exact[0]["id"], "search"
        if not cid:
            upsert(c, key, name, tmdb_match="none", tmdb_checked=time.time(), tmdb_error=None); c.commit(); return "none"
        try: co = tm(f"company/{cid}", api_key)
        except urllib.error.HTTPError as ex:
            if ex.code != 404: raise
            upsert(c, key, name, tmdb_company_id=cid, tmdb_match=how, tmdb_checked=time.time(), tmdb_error="TMDB has no company " + str(cid)); c.commit(); return "none"
        d = tm("discover/movie", api_key, with_companies=cid, include_adult="true", sort_by="primary_release_date.asc")
        par = co.get("parent_company")
        upsert(c, key, name, tmdb_company_id=cid, tmdb_match=how, tmdb_checked=time.time(), tmdb_error=None, tmdb_name=co.get("name"),
               logo=co.get("logo_path") or None, origin_country=(co.get("origin_country") or "").strip() or None,
               headquarters=(co.get("headquarters") or "").strip() or None, description=(co.get("description") or "").strip() or None,
               homepage=(co.get("homepage") or "").strip() or None, parent=(par.get("name") if isinstance(par, dict) else (par or None)),
               tmdb_total=d.get("total_results"))
        c.commit(); return "tmdb"

    # ---------- ThePornDB site (scene studios) ----------
    def lookup_tpdb(c, key, name, token):
        names = tpdbmod.studio_group(name) or [name]; ks = {tpdbmod.nrm(n) for n in names}
        site = None
        for q in names[:3]:
            for s in (tpdbmod.call_retry("/sites", token, q=q).get("data") or [])[:10]:
                if tpdbmod.nrm(s.get("name")) in ks or tpdbmod.nrm(s.get("short_name")) in ks: site = s; break
            if site: break
        if not site:
            upsert(c, key, name, tpdb_checked=time.time(), tpdb_error=None, tpdb_site_id=None); c.commit(); return "none"
        meta = tpdbmod.call_retry("/scenes", token, site_id=site["id"], per_page=1).get("meta") or {}
        par = site.get("parent") or site.get("network") or {}
        upsert(c, key, name, tpdb_site_id=str(site["id"]), tpdb_checked=time.time(), tpdb_error=None, tpdb_name=site.get("name"),
               tpdb_url=site.get("url") or None, tpdb_logo=site.get("logo") or None, tpdb_desc=(site.get("description") or "").strip() or None,
               tpdb_parent=(par.get("name") if isinstance(par, dict) else None), tpdb_total=meta.get("total"))
        c.commit(); return "tpdb"

    def lookup_one(c, key, name, fids, kinds, api_key, token):
        """TMDB for studios with films; ThePornDB for scene-only studios (TMDB is never used for scenes)."""
        scene_only = bool(fids) and all(kinds.get(f) == "scene" for f in fids)
        if scene_only:
            if not token: return "skip"
            return lookup_tpdb(c, key, name, token)
        if not api_key: return "skip"
        return lookup_tmdb(c, key, name, fids, api_key)

    # ---------- background backfill (every studio in the library) ----------
    def run_job():
        with LOCK:
            if JOB["running"]: return False
            JOB.update(running=True, stop=False, done=0, total=0, tmdb=0, tpdb=0, none=0, errors=0, current=None,
                       started_at=time.time(), finished_at=None, last_error=None)
        def work():
            c = C()
            try:
                api_key, token = keys_of(c)
                load_countries(c, api_key)
                titles, display, _ = whole(c)
                kinds = {r[0]: r[1] for r in c.execute("SELECT id, kind FROM films")}
                done = {r[0]: r for r in c.execute("SELECT key, tmdb_checked, tpdb_checked FROM studio_info")}
                q = []
                for k, fids in titles.items():
                    r = done.get(k)
                    scene_only = all(kinds.get(f) == "scene" for f in fids)
                    if scene_only and not token: continue
                    if not scene_only and not api_key: continue
                    if r is not None and (r[2] if scene_only else r[1]): continue
                    q.append((-len(fids), display[k].lower(), k))
                q.sort(); JOB["total"] = len(q); fails = 0
                for _, _, k in q:
                    if JOB["stop"]: break
                    nm = display[k]; JOB["current"] = nm
                    try:
                        res = lookup_one(c, k, nm, sorted(titles[k]), kinds, api_key, token)
                        if res in JOB: JOB[res] += 1
                        fails = 0
                    except Exception as ex:
                        JOB["errors"] += 1; fails += 1; JOB["last_error"] = f"{nm}: {type(ex).__name__}"
                        col = "tpdb_error" if all(kinds.get(f) == "scene" for f in titles[k]) else "tmdb_error"
                        try: upsert(c, k, nm, **{col: f"{type(ex).__name__}: {str(ex)[:120]}"}); c.commit()
                        except Exception: pass
                        if isinstance(ex, urllib.error.HTTPError) and ex.code == 401:
                            JOB["last_error"] = "API key rejected (401); stopped"; break
                        if fails >= 8:
                            JOB["last_error"] += " (stopped: offline or the site keeps failing; resumes next start)"; break
                        time.sleep(min(30, 2 * fails))
                    JOB["done"] += 1
            finally:
                JOB.update(running=False, current=None, finished_at=time.time()); c.close()
        threading.Thread(target=work, daemon=True).start()
        return True

    def job_counts(c):
        titles, _, _ = whole(c)
        kinds = {r[0]: r[1] for r in c.execute("SELECT id, kind FROM films")}
        rows = {r[0]: r for r in c.execute("SELECT key, tmdb_checked, tpdb_checked FROM studio_info")}
        checked = sum(1 for k, f in titles.items() if rows.get(k) is not None and
                      (rows[k][2] if all(kinds.get(x) == "scene" for x in f) else rows[k][1]))
        return dict(studios=len(titles), checked=checked)

    @app.get("/api/studios/backfill")
    def studios_backfill_status():
        c = C()
        return jsonify(job={k: v for k, v in JOB.items() if k != "stop"}, auto=bool(db.get_setting(c, "studio_backfill_auto", True)), **job_counts(c))

    @app.post("/api/studios/backfill")
    def studios_backfill_start():
        d = request.get_json(silent=True) or {}; c = C()
        if d.get("auto") is not None:
            db.set_setting(c, "studio_backfill_auto", bool(d["auto"])); c.commit()
            if not d["auto"]: JOB["stop"] = True; return jsonify(ok=True, started=False)
        if d.get("stop"): JOB["stop"] = True; return jsonify(ok=True, stopped=True)
        return jsonify(ok=True, started=run_job())

    def auto():
        time.sleep(120)
        c = C()
        try: on = bool(db.get_setting(c, "studio_backfill_auto", True))
        finally: c.close()
        if on: run_job()
    if enabled(): threading.Thread(target=auto, daemon=True).start()

    # ---------- catalog (the studio's whole filmography / scene list) for one studio page ----------
    def page_job(key, name, fids, kinds, refresh=False):
        st = PAGEJOBS.get(key)
        if st and st.get("running"): return st
        if st and not refresh and time.time() - st.get("finished", 0) < 300: return st  # no hammering after a failure
        st = PAGEJOBS[key] = dict(running=True, phase="info", done=0, total=0, error=None, finished=0)
        def work():
            c = C()
            try:
                api_key, token = keys_of(c)
                load_countries(c, api_key)
                row = row_of(c, key)
                scene_only = bool(fids) and all(kinds.get(f) == "scene" for f in fids)
                need = row is None or refresh or not (row["tpdb_checked"] if scene_only else row["tmdb_checked"])
                if need:
                    if scene_only and not token: st["error"] = "No ThePornDB API key is set."; return
                    if not scene_only and not api_key: st["error"] = "No TMDB API key is set."; return
                    lookup_one(c, key, name, fids, kinds, api_key, token); row = row_of(c, key)
                st["phase"] = "catalog"
                items, total, src = [], None, None
                if row is not None and row["tmdb_company_id"] and not scene_only:
                    src = "tmdb"; page = 1
                    while page <= TMDB_PAGES:
                        d = tm("discover/movie", api_key, with_companies=row["tmdb_company_id"], include_adult="true",
                               sort_by="primary_release_date.asc", page=page)
                        total = d.get("total_results"); pages = min(d.get("total_pages") or 1, TMDB_PAGES)
                        st.update(total=total or 0)
                        for m in d.get("results") or []:
                            rd = (m.get("release_date") or "")[:10]
                            items.append(dict(id=m.get("id"), title=m.get("title") or m.get("original_title"), date=rd or None,
                                              year=int(rd[:4]) if rd[:4].isdigit() else None, adult=bool(m.get("adult")),
                                              thumb=("https://image.tmdb.org/t/p/w154" + m["poster_path"]) if m.get("poster_path") else None))
                        st["done"] = len(items)
                        if page >= pages: break
                        page += 1
                elif row is not None and row["tpdb_site_id"] and scene_only and token:
                    src = "tpdb"; page = 1
                    while page <= TPDB_PAGES:
                        d = tpdbmod.call_retry("/scenes", token, site_id=row["tpdb_site_id"], per_page=100, page=page)
                        meta = d.get("meta") or {}; total = meta.get("total"); st.update(total=total or 0)
                        for sc in d.get("data") or []:
                            b = tpdbmod.brief(sc)
                            items.append(dict(id=b["id"], title=b["title"], date=b["date"] or None,
                                              year=int(b["date"][:4]) if (b["date"] or "")[:4].isdigit() else None, url=b["url"], thumb=b.get("thumb")))
                        st["done"] = len(items)
                        if page >= (meta.get("last_page") or 1): break
                        page += 1
                if src:
                    c.execute("INSERT OR REPLACE INTO studio_catalog(key, src, items_json, total, capped, fetched_at) VALUES(?,?,?,?,?,?)",
                              (key, src, json.dumps(items), total, int(bool(total and total > len(items))), time.time()))
                    upsert(c, key, name, **({"tmdb_total": total} if src == "tmdb" else {"tpdb_total": total})); c.commit()
            except urllib.error.HTTPError as ex:
                st["error"] = "The API key was rejected (401)." if ex.code == 401 else f"The site answered HTTP {ex.code}."
            except Exception as ex:
                st["error"] = "Could not reach TMDB / ThePornDB (offline?)." if isinstance(ex, (urllib.error.URLError, TimeoutError, OSError)) else f"{type(ex).__name__}: {str(ex)[:160]}"
            finally:
                st.update(running=False, finished=time.time()); c.close()
        threading.Thread(target=work, daemon=True).start()
        return st

    def lib_where(lib):
        return build_query({"lib": lib}) if lib else ("", [])

    # ---------- Studios list ----------
    @app.get("/api/studios/list")
    def studios_list():
        """Every studio with titles in the library (or in the chosen library folder, ?lib=, same folder match as the film
        list): name, country (hand / TMDB / inferred), years active, titles in library and the catalog total."""
        c = C(); lib = (request.args.get("lib") or "").strip().rstrip("\\/")
        where, args = lib_where(lib)
        titles, display, ids = repmod.studio_titles(c, f"SELECT f.id FROM films f {where}", args)
        wt = whole(c)[0] if lib else titles
        F = {r[0]: (r[1], r[2], r[3]) for r in c.execute("SELECT id, year, country, kind FROM films")}
        cty = {k: v[1] for k, v in F.items()}
        info = {r["key"]: r for r in c.execute("SELECT * FROM studio_info")}
        cmap = country_names(c); out = []; cc = Counter()
        for k, fids in titles.items():
            ys = [F[f][0] for f in fids if f in F and F[f][0] and F[f][0] > 0]
            row = info.get(k); inf = inferred_country(cty, wt.get(k, fids))
            name, iso, src = country_of(row, inf, cmap)
            scenes = sum(1 for f in fids if F.get(f, (0, 0, ""))[2] == "scene")
            tot, tsrc = None, None
            if row is not None:
                if row["tmdb_total"] is not None and scenes < len(fids): tot, tsrc = row["tmdb_total"], "tmdb"
                elif row["tpdb_total"] is not None: tot, tsrc = row["tpdb_total"], "tpdb"
            logo = (TMDB_IMG + row["logo"]) if row is not None and row["logo"] else (row["tpdb_logo"] if row is not None else None)
            out.append(dict(name=display[k], key=k, n=len(fids), scenes=scenes, year_min=min(ys) if ys else None, year_max=max(ys) if ys else None,
                            country=name, iso=iso, country_src=src, total=tot, total_src=tsrc, logo=logo,
                            checked=bool(row is not None and (row["tmdb_checked"] or row["tpdb_checked"]))))
            cc[name or ""] += 1
        out.sort(key=lambda r: db.fold(r["name"]))
        have = set().union(*titles.values()) if titles else set()
        countries = [dict(name=k, iso=iso_of(k, cmap), n=v) for k, v in sorted(cc.items(), key=lambda kv: (-kv[1], db.fold(kv[0]))) if k]
        return jsonify(studios=out, total=len(out), titles=len(ids), no_studio=len(ids - have), countries=countries,
                       no_country=cc.get("", 0), lib=lib, job={k: v for k, v in JOB.items() if k != "stop"})

    # ---------- Studio page ----------
    @app.get("/api/studio")
    def studio_page():
        name = " ".join((request.args.get("name") or "").split())
        if not name: return jsonify(error="No studio given"), 400
        lib = (request.args.get("lib") or "").strip().rstrip("\\/")
        c = C(); k = main_key(c, name)
        if not k: return jsonify(error="No studio given"), 400
        titles, display, _ = whole(c)
        amap = db.studio_alias_map(c)
        main = display.get(k) or next((v for v in amap.values() if db.studio_key(v) == k), None) or name
        fids = sorted(titles.get(k, ()))
        kinds = {r[0]: r[1] for r in c.execute("SELECT id, kind FROM films")}
        q = {"studio": main}
        if lib: q["lib"] = lib
        where, args = build_query(q)
        ex = repmod.explore(c, where, args, lim=15)
        lst = [dict(id=r["id"], title=r["title"], year=r["year"], date=r["release_date"], director=r["director"], kind=r["kind"] or "film",
                    poster=r["custom_poster"] or r["poster"] or r["poster_wide"], tmdb_id=r["tmdb_id"], tpdb_id=r["tpdb_id"],
                    genre=r["primary_genre"], country=r["country"])
               for r in c.execute(f"SELECT f.* FROM films f {where} ORDER BY f.year IS NULL, f.year, f.release_date, f.title COLLATE NOCASE", args)]
        row = row_of(c, k); cmap = country_names(c)
        cty = {r[0]: r[1] for r in c.execute(f"SELECT id, country FROM films WHERE id IN ({','.join(map(str, fids)) or 'NULL'})")}
        inf = inferred_country(cty, fids)
        cname, iso, csrc = country_of(row, inf, cmap)
        scene_only = bool(fids) and all(kinds.get(f) == "scene" for f in fids)
        aliases = [a for a in db.studio_names(main, c) if a != main]  # hidden spellings (Settings > Studios, or + Alias here)
        links = db.studio_links(c)
        ks = {db.studio_key(n) for n in db.studio_names(main, c)} | {k}
        parents = [l["parent"] for l in links if db.studio_key(l["child"]) in ks]
        children = [l["child"] for l in links if db.studio_key(l["parent"]) in ks]
        # catalog: cached; loaded in the background when missing or older than CATALOG_DAYS
        cat = c.execute("SELECT * FROM studio_catalog WHERE key=?", (k,)).fetchone()
        st = PAGEJOBS.get(k)
        refresh = bool(request.args.get("refresh"))
        stale = cat is None or time.time() - (cat["fetched_at"] or 0) > CATALOG_DAYS * 86400
        unchecked = row is None or not (row["tpdb_checked"] if scene_only else row["tmdb_checked"])
        has_src = row is not None and ((row["tpdb_site_id"] and scene_only) or (row["tmdb_company_id"] and not scene_only))
        api_key, token = keys_of(c)
        online_ok = bool(token if scene_only else api_key)
        if fids and online_ok and (refresh or unchecked or (stale and has_src)):
            st = page_job(k, main, fids, kinds, refresh=refresh)
        catalog = None
        if cat is not None and not (st and st.get("running") and refresh):
            items = json.loads(cat["items_json"] or "[]")
            if cat["src"] == "tmdb":
                own = {r[0]: r[1] for r in c.execute("SELECT tmdb_id, id FROM films WHERE tmdb_id IS NOT NULL")}
            else:
                own = {str(r[0]): r[1] for r in c.execute("SELECT tpdb_id, id FROM films WHERE COALESCE(tpdb_id,'')<>''")}
            for it in items: it["film_id"] = own.get(it["id"] if cat["src"] == "tmdb" else str(it["id"]))
            catalog = dict(src=cat["src"], items=items, total=cat["total"], capped=bool(cat["capped"]), fetched_at=cat["fetched_at"],
                           owned=sum(1 for it in items if it["film_id"]))
        loading = dict(phase=st["phase"], done=st["done"], total=st["total"]) if st and st.get("running") else None
        info = None
        if row is not None:
            info = dict(tmdb_company_id=row["tmdb_company_id"], tmdb_match=row["tmdb_match"], tmdb_name=row["tmdb_name"],
                        tmdb_url=f"https://www.themoviedb.org/company/{row['tmdb_company_id']}" if row["tmdb_company_id"] else None,
                        logo=(TMDB_IMG + row["logo"]) if row["logo"] else row["tpdb_logo"], headquarters=row["headquarters"],
                        parent=row["parent"] or row["tpdb_parent"], description=row["description"] or row["tpdb_desc"],
                        homepage=row["homepage"] or row["tpdb_url"], tmdb_total=row["tmdb_total"], tpdb_total=row["tpdb_total"],
                        tpdb_site_id=row["tpdb_site_id"], tpdb_name=row["tpdb_name"], tmdb_checked=row["tmdb_checked"], tpdb_checked=row["tpdb_checked"],
                        error=row["tpdb_error"] if scene_only else row["tmdb_error"], edited=sorted(_edited(row)))
        total = None
        if row is not None: total = row["tpdb_total"] if scene_only else row["tmdb_total"]
        ys = [x["year"] for x in lst if x["year"] and x["year"] > 0]
        return jsonify(name=main, key=k, lib=lib, aliases=aliases, parents=parents, children=children, scene_only=scene_only,
                       country=cname, iso=iso, country_src=csrc, inferred=inf, info=info, total=total,
                       total_src=("tpdb" if scene_only else "tmdb") if total is not None else None,
                       in_library=ex["total"], library_all=len(fids), years=[min(ys), max(ys)] if ys else None,
                       stats=ex, titles=lst, catalog=catalog, loading=loading, error=(st or {}).get("error"),
                       online=online_ok)

    @app.post("/api/studio/edit")
    def studio_edit():
        """{name, country?: 'Sweden' ('' = back to automatic), tmdb?: id or TMDB company URL ('' = automatic)}.
        Hand edits are kept by the background job and refreshes."""
        d = request.get_json(silent=True) or {}
        name = " ".join((d.get("name") or "").split())
        c = C(); k = main_key(c, name)
        if not k: return jsonify(error="No studio given"), 400
        _, display, _ = whole(c); main = display.get(k) or name
        row = row_of(c, k); ed = _edited(row); f = {}
        if "country" in d:
            v = " ".join((d.get("country") or "").split())
            if v: ed.add("country"); f["country"] = v
            else: ed.discard("country"); f["country"] = None
        if "tmdb" in d:
            v = str(d.get("tmdb") or "").strip()
            if v:
                m = re.search(r"company/(\d+)", v) or re.fullmatch(r"\s*(\d+)\s*", v)
                if not m: return jsonify(error="Not a TMDB company id or URL"), 400
                ed.add("tmdb"); f.update(tmdb_company_id=int(m.group(1)), tmdb_match="manual", tmdb_checked=None)
            else:
                ed.discard("tmdb"); f.update(tmdb_company_id=None, tmdb_match=None, tmdb_checked=None)
            c.execute("DELETE FROM studio_catalog WHERE key=?", (k,)); PAGEJOBS.pop(k, None)
        f["edited_fields"] = json.dumps(sorted(ed))
        upsert(c, k, main, **f); c.commit()
        return jsonify(ok=True)
