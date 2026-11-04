"""'Find birth date': candidate birth dates for one person from ThePornDB, TMDB, Wikidata (P569) and the English Wikipedia
infobox, each checked against the person's library titles (age at every release; under 18 = impossible). Nothing is
saved here: the review popup's Accept goes through the normal Edit profile path (a protected hand edit).
GET /api/people/<id>/find_birth"""
import re, json, time, datetime, concurrent.futures
import db, birthdates as bd

BUDGET = 8.0      # seconds per source (the sources run side by side, so a lookup takes about this long at most)
MAX_NAMES = 2     # name + first alias searched on Wikidata / TMDB (ThePornDB: only when there is no linked id)
ADULT_GENRES = ("hardcore", "softcore")  # same as the person page age rule (people.person_rows)

def _dt(s):
    try: return datetime.date.fromisoformat(str(s)[:10])
    except Exception: return None

def _age(b, d): return d.year - b.year - ((d.month, d.day) < (b.month, b.day))

def names_of(c, row):
    out = []
    for n in [row["name"]] + list(db.expand_person(c, row["name"])):
        if n and n not in out: out.append(n)
    try:
        for a in json.loads(row["aliases"] or "[]"):
            if isinstance(a, str) and a.strip() and a not in out: out.append(a.strip())
    except Exception: pass
    return out

def library_titles(c, names):
    """Library titles with their date and whether they are adult (the app's age rule: scene, adult library folder, or a
    Hardcore / Softcore primary genre). Only adult titles are checked for age."""
    import people as P
    adult_ids = P.adult_film_ids(c)
    sel = " OR ".join(f"person_in_any(f.{col}, ?)" for col in db.PEOPLE_ROLES.values())
    out = []
    for r in c.execute(f"SELECT f.id, f.title, f.release_date, f.year, f.kind, f.primary_genre FROM films f WHERE {sel}", [db.ALIAS_SEP.join(names)] * len(db.PEOPLE_ROLES)):
        d = _dt(r["release_date"]) or (datetime.date(int(r["year"]), 1, 1) if r["year"] else None)
        adult = r["id"] in adult_ids or r["kind"] == "scene" or (r["primary_genre"] or "").strip().lower() in ADULT_GENRES
        out.append(dict(id=r["id"], title=r["title"], date=d.isoformat() if d else None, exact=bool(_dt(r["release_date"])), adult=adult))
    return sorted(out, key=lambda t: t["date"] or "9999")

def check(cand, titles):
    """Adds age_first, impossible (+why), year_only to a candidate."""
    dated = [t for t in titles if t["date"] and t.get("adult")]  # age rule: adult titles only
    if cand.get("date"):
        b = _dt(cand["date"]); ages = [(_age(b, _dt(t["date"])), t) for t in dated]
        cand["year_only"] = False
    else:
        y = int(cand["year"]); ages = [(int(t["date"][:4]) - y - 1, t) for t in dated]  # youngest she could have been
        cand["year_only"] = True
    if ages:
        a, t = min(ages, key=lambda x: x[0])
        cand["age_first"] = a; cand["first_title"] = t["title"]; cand["first_date"] = t["date"]
        if a < 18 and not (cand["year_only"] and int(t["date"][:4]) - int(cand["year"]) >= 18):
            cand["impossible"] = True
            cand["why"] = f"would be {a} at “{t['title']}” ({t['date']})" + (" (year only: at most " + str(int(t['date'][:4]) - int(cand['year'])) + ")" if cand["year_only"] else "")
    cand.setdefault("impossible", False)
    return cand

def _wd_cands(names, deadline=None):
    out, seen = [], set()
    late = lambda: deadline and time.time() > deadline
    for n in names[:MAX_NAMES]:
        if late(): break
        s = bd._get(bd.WD_API, {"action": "wbsearchentities", "search": n, "language": "en", "type": "item", "limit": 10, "format": "json"})
        ids = [h["id"] for h in s.get("search", []) if h["id"] not in seen]
        seen.update(ids)
        ents = bd._entities(ids[:10]) if ids else {}
        k = db.fold(" ".join(n.split()))
        for qid, ent in ents.items():
            if "Q5" not in bd._claim_ids(ent, "P31") or k not in bd._names(ent): continue
            if not set(bd._claim_ids(ent, "P106")) & bd.FILM_OCC: continue  # film / adult-film people only (same list as the birth-date job)
            full, year = bd._dob(ent)
            desc = ((ent.get("descriptions") or {}).get("en") or {}).get("value", "")
            if full or year:
                out.append(dict(source="Wikidata", url=f"https://www.wikidata.org/wiki/{qid}", date=full, year=year if not full else int(full[:4]),
                                matched=n, note=desc))
            wt = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
            if wt and not late():
                try: wb, title, _ = bd._wp_birth(wt)
                except Exception: wb, title = None, None
                if wb: out.append(dict(source="Wikipedia", url="https://en.wikipedia.org/wiki/" + urllib_q(title or wt), date=wb, year=int(wb[:4]), matched=n, note=f"infobox (linked from {qid})"))
    return out

def urllib_q(t):
    import urllib.parse
    return urllib.parse.quote(t.replace(" ", "_"))

def find(c, row, tmdb=None, tpdb_call=None, tmdb_key="", tpdb_tok=""):
    import people as P
    names = names_of(c, row); titles = library_titles(c, names); cands, errors = [], []
    deadline = time.time() + BUDGET
    def src_tpdb():
        out = []
        if not (tpdb_tok and tpdb_call): return out
        perfs = []
        if row["tpdb_performer_id"] not in (None, P.TPDB_NONE):  # linked: no name search
            p = (tpdb_call(f"/performers/{row['tpdb_performer_id']}").get("data") or {}); p and perfs.append((p, row["name"]))
        else:
            for n in names[:MAX_NAMES]:
                if time.time() > deadline: break
                hit = P.pick_performer(tpdb_call("/performers", q=n).get("data") or [], n)
                if hit and all(str(hit.get("id")) != str(x.get("id")) for x, _ in perfs):
                    full = hit if (hit.get("extras") or {}).get("birthday") else (tpdb_call(f"/performers/{hit['id']}").get("data") or hit)
                    perfs.append((full, n))
        for p, n in perfs:
            b = ((p.get("extras") or {}).get("birthday") or "")[:10]
            if _dt(b): out.append(dict(source="ThePornDB", url=f"https://theporndb.net/performers/{p.get('slug') or p.get('id')}", date=b, year=int(b[:4]), matched=p.get("name") or n))
        return out
    def src_tmdb():
        out = []
        if not (tmdb and tmdb_key): return out
        ids = []
        if (row["tmdb_person_id"] or 0) > 0: ids = [(row["tmdb_person_id"], row["name"])]
        else:
            for n in names[:MAX_NAMES]:
                if time.time() > deadline: break
                k = db.fold(" ".join(n.split()))
                for r in (tmdb("search/person", tmdb_key, query=n, include_adult="true").get("results") or [])[:10]:
                    if db.fold(" ".join((r.get("name") or "").split())) == k and r["id"] not in [i for i, _ in ids]: ids.append((r["id"], n))
        for pid, n in ids[:3]:
            if time.time() > deadline: break
            p = tmdb(f"person/{pid}", tmdb_key); b = (p.get("birthday") or "")[:10]
            if _dt(b): out.append(dict(source="TMDB", url=f"https://www.themoviedb.org/person/{pid}", date=b, year=int(b[:4]), matched=p.get("name") or n))
        return out
    srcs = [("ThePornDB", src_tpdb), ("TMDB", src_tmdb), ("Wikidata", lambda: _wd_cands(names, deadline))]
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    futs = {ex.submit(f): lbl for lbl, f in srcs}
    done, pending = concurrent.futures.wait(futs, timeout=BUDGET + 1.5)
    for f, lbl in futs.items():  # keep the source order
        if f in pending: errors.append(f"{lbl}: no answer within {int(BUDGET)} s"); continue
        try: cands += f.result()
        except Exception as e: errors.append(f"{lbl}: {str(e)[:80]}")
    ex.shutdown(wait=False)
    order = {"ThePornDB": 0, "TMDB": 1, "Wikidata": 2, "Wikipedia": 2}
    cands.sort(key=lambda x: order.get(x["source"], 3))
    for x in cands: check(x, titles)
    full = {x["date"] for x in cands if x.get("date")}
    years = {x["year"] for x in cands}
    conflict = len(full) > 1 or len(years) > 1
    for x in cands: x["conflict"] = conflict
    return dict(id=row["id"], name=row["name"], names=names, titles=len(titles), first_title=next((t for t in titles if t["date"] and t["adult"]), None), adult_titles=sum(1 for t in titles if t["adult"]),
                candidates=cands, conflict=conflict, errors=errors)

def init(app, connect, tmdb):
    from flask import jsonify
    @app.get("/api/people/<int:pid>/find_birth")
    def find_birth(pid):
        import tpdb as tpdbmod
        c = connect(); row = c.execute("SELECT * FROM people WHERE id=?", (pid,)).fetchone()
        if not row: return jsonify(error="Unknown person"), 404
        tok = db.get_setting(c, "tpdb_key", "") or ""; key = db.get_setting(c, "tmdb_key", "") or ""
        try: res = find(c, row, tmdb, lambda path, **kw: tpdbmod.call_retry(path, tok, **kw), key, tok)
        finally: c.close()
        res["birthday"] = row["birthday"]; return jsonify(res)
