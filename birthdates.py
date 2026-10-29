"""Birth dates from Wikidata (P569) / Wikipedia when ThePornDB and TMDB don't have one, then free fallback sites
(Boobpedia, FreeOnes, Babepedia: bdsources.py) when Wikidata / Wikipedia have none or only a year.

Only confident, unique matches are saved (people.birthday + people.birth_date_source, e.g. 'wikidata Q465186');
hand-edited or existing birth dates are never overwritten. Ambiguous matches go to a review list.
Background job over everyone credited in the library without a birth date (adult performers first), rate-limited
and resumable (table birthdate_lookup), plus single lookups when a person page loads."""
import re, json, time, datetime, threading, urllib.request, urllib.parse, urllib.error
import db, bdsources

UA = "FilmTagger/1.3 (https://github.com/kylerhudson1975/film-tagger; birth-date lookup)"
WD_API, WD_SPARQL, WP_API = "https://www.wikidata.org/w/api.php", "https://query.wikidata.org/sparql", "https://en.wikipedia.org/w/api.php"
FILM_OCC = {"Q33999", "Q10800557", "Q488111", "Q2526255", "Q3455803", "Q10798782", "Q2259451", "Q4610556", "Q28389", "Q3282637",
            "Q222344", "Q36834", "Q2405480", "Q1053574", "Q7042855", "Q947873", "Q15077007", "Q18844224", "Q1415090", "Q2722764",
            "Q5716684", "Q158852", "Q1028181", "Q11481802", "Q245068", "Q639669", "Q753110", "Q2490358", "Q193391"}
PORN_OCC = "Q488111"
SCHEMA = """CREATE TABLE IF NOT EXISTS birthdate_lookup(name_norm TEXT PRIMARY KEY, name TEXT, status TEXT, birthday TEXT,
  source TEXT, detail TEXT, at REAL);"""
JOB = dict(running=False, stop=False, done=0, total=0, filled=0, ambiguous=0, not_found=0, no_birthday=0, errors=0,
           current=None, started_at=None, finished_at=None, last_error=None, by_source={}, rechecked=0)
FB_FORCE = [False]  # tests: run the fallback step (with bdsources.lookup stubbed) on a temp DB
FB_RETRY = ("not_found", "no_birthday", "ambiguous")  # statuses the fallback sites are tried for (once: birthdate_lookup.fb_at)
LOCK = threading.Lock()
_last = [0.0]
TMDB = {"fn": None}

def ensure(c):
    import people
    c.executescript(SCHEMA); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)  # people.migrate adds birth_date_source
    if "fb_at" not in {r[1] for r in c.execute("PRAGMA table_info(birthdate_lookup)")}:
        c.execute("ALTER TABLE birthdate_lookup ADD COLUMN fb_at REAL")  # when the fallback sites were tried
    c.commit()

def _get(url, params=None, accept="application/json", tries=4):
    if params: url += "?" + urllib.parse.urlencode(params)
    for i in range(tries):
        with LOCK:  # ~1 request / 0.6 s across the job and page lookups (Wikimedia etiquette)
            w = 0.6 - (time.time() - _last[0])
            if w > 0: time.sleep(w)
            _last[0] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept}), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(120.0, float(ra)) if ra and ra.replace(".", "").isdigit() else 10 * (i + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if i < tries - 1: time.sleep(5 * (i + 1)); continue
            raise

def _sparql(q): return _get(WD_SPARQL, {"query": q, "format": "json"}, "application/sparql-results+json")

def _claim_ids(ent, p): return [s["mainsnak"]["datavalue"]["value"]["id"] for s in ent.get("claims", {}).get(p, []) if s.get("mainsnak", {}).get("datavalue")]

def _dob(ent):
    """(YYYY-MM-DD or None, birth year or None) from P569; prefers the best-ranked day-precision value."""
    best, year = None, None
    for s in sorted(ent.get("claims", {}).get("P569", []), key=lambda s: s.get("rank") != "preferred"):
        if s.get("rank") == "deprecated": continue
        v = (s.get("mainsnak", {}).get("datavalue") or {}).get("value") or {}
        t, pr = v.get("time") or "", v.get("precision") or 0
        m = re.match(r"^\+(\d{4})-(\d\d)-(\d\d)", t)
        if not m: continue
        year = year or int(m.group(1))
        if pr >= 11 and not best:
            try: best = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError: pass
    return best, year

def _names(ent):
    out = set()
    for v in (ent.get("labels") or {}).values(): out.add(db.fold(v["value"]))
    for l in (ent.get("aliases") or {}).values():
        for v in l: out.add(db.fold(v["value"]))
    return out

def _entities(ids):
    if not ids: return {}
    d = _get(WD_API, {"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels|aliases|descriptions|sitelinks",
                      "languages": "en|fr|es|it|de|pt|nl|sv|da|ca", "sitefilter": "enwiki", "format": "json"})
    return d.get("entities") or {}

def _wp_birth(title):
    """Birth date from an English Wikipedia infobox ({{birth date|Y|M|D}} variants), with the page lead for sanity checks."""
    d = _get(WP_API, {"action": "query", "prop": "revisions|pageprops", "rvprop": "content", "rvslots": "main", "titles": title,
                      "redirects": 1, "format": "json", "formatversion": 2})
    pg = ((d.get("query") or {}).get("pages") or [{}])[0]
    if pg.get("missing") or "disambiguation" in (pg.get("pageprops") or {}): return None, None, ""
    txt = (((pg.get("revisions") or [{}])[0].get("slots") or {}).get("main") or {}).get("content") or ""
    m = re.search(r"\|\s*birth_date\s*=\s*\{\{\s*(?:birth[ _]date(?:[ _]and[ _]age)?|bda|dob)\s*\|(?:\s*(?:df|mf)\s*=\s*\w+\s*\|)?\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})", txt, re.I)
    bd = None
    if m:
        try: bd = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError: bd = None
    lead = re.sub(r"\{\{[^{}]*\}\}", "", txt)[:6000]
    return bd, pg.get("title"), lead

def lookup(name, tmdb_person=None, film_tmdb_ids=(), years=(), adult=False):
    """-> dict(status, birthday?, source?, detail). status: filled | ambiguous | not_found | no_birthday."""
    k = db.fold(" ".join(name.split()))
    y0 = min(years) if years else None
    plaus = lambda by: by is None or y0 is None or 12 <= y0 - by <= 90
    # 1) exact TMDB person id on Wikidata (P4985)
    if tmdb_person:
        r = _sparql(f'SELECT ?p WHERE {{ ?p wdt:P4985 "{int(tmdb_person)}" }} LIMIT 3')
        q = [b["p"]["value"].rsplit("/", 1)[1] for b in r["results"]["bindings"]]
        if len(q) == 1:
            ent = _entities(q)[q[0]]; bd, by = _dob(ent)
            if bd: return dict(status="filled", birthday=bd, source=f"wikidata {q[0]}", detail="TMDB person id on Wikidata")
            wb = _wiki_fallback(ent, plaus)
            if wb: return wb
            return dict(status="no_birthday", detail=f"{q[0]} has no exact birth date", year=by)
    # 2) name search: humans with a film/TV occupation whose label or alias is this exact name
    hits, crowded = [], False
    for lang in ("en", "fr"):
        s = _get(WD_API, {"action": "wbsearchentities", "search": name, "language": lang, "uselang": lang, "type": "item", "limit": 20, "format": "json"})
        hits += [h["id"] for h in s.get("search", []) if h["id"] not in hits]
        crowded = crowded or "search-continue" in s  # more than 20 items share this name: a lone match needs credit evidence
        if hits: break
    ents = _entities(hits[:20])
    cands = []
    for qid, ent in ents.items():
        if "Q5" not in _claim_ids(ent, "P31"): continue
        if k not in _names(ent): continue
        occ = set(_claim_ids(ent, "P106"))
        if not occ & FILM_OCC: continue
        bd, by = _dob(ent)
        cands.append(dict(qid=qid, ent=ent, bd=bd, by=by, porn=PORN_OCC in occ, desc=((ent.get("descriptions") or {}).get("en") or {}).get("value", "")))
    if not cands:
        bd, title, lead = _wp_birth(name)
        if bd and title and db.fold(title.split(" (")[0]) == k and re.search(r"\b(actress|actor|film director|filmmaker|pornographic|adult film|screenwriter)\b", lead[:3000], re.I) \
                and plaus(int(bd[:4])):
            return dict(status="filled", birthday=bd, source=f"wikipedia {title}", detail="English Wikipedia infobox (no Wikidata match)")
        return dict(status="not_found", detail=f"{len(hits)} Wikidata results, none a film person named exactly this")
    pick, why = None, ""
    if (len(cands) > 1 or crowded) and film_tmdb_ids:  # credits overlap: films in your library (by TMDB id) that list the candidate
        vals = " ".join(f'"{int(t)}"' for t in list(film_tmdb_ids)[:80]); ps = " ".join("wd:" + x["qid"] for x in cands)
        r = _sparql(f"SELECT ?p (COUNT(DISTINCT ?f) AS ?n) WHERE {{ VALUES ?p {{ {ps} }} VALUES ?t {{ {vals} }} ?f wdt:P4947 ?t . "
                    f"?f (wdt:P161|wdt:P57|wdt:P58|wdt:P162|wdt:P344|wdt:P86|wdt:P1040) ?p . }} GROUP BY ?p")
        ov = {b["p"]["value"].rsplit("/", 1)[1]: int(b["n"]["value"]) for b in r["results"]["bindings"]}
        withov = [x for x in cands if ov.get(x["qid"])]
        if len(withov) == 1: pick, why = withov[0], f"credited on {ov[withov[0]['qid']]} of your films on Wikidata"
        elif len(withov) > 1: cands = withov
    if not pick and crowded:
        return dict(status="ambiguous", detail=json.dumps([dict(qid=x["qid"], birthday=x["bd"], year=x["by"], desc=x["desc"]) for x in cands[:6]] +
                                                          ([dict(note="common name: more than 20 Wikidata items")] if len(cands) == 1 else [])))
    if not pick:
        pl = [x for x in cands if plaus(x["by"])]
        if len(pl) == 1: pick, why = pl[0], "only matching film person" + (" (career years fit)" if len(cands) > 1 else "")
        elif adult and len([x for x in pl if x["porn"]]) == 1: pick = [x for x in pl if x["porn"]][0]; why = "only adult-film performer with this name"
        elif pl: cands = pl
    if not pick:
        return dict(status="ambiguous", detail=json.dumps([dict(qid=x["qid"], birthday=x["bd"], year=x["by"], desc=x["desc"]) for x in cands[:6]]))
    if pick["bd"]: return dict(status="filled", birthday=pick["bd"], source=f"wikidata {pick['qid']}", detail=why)
    wb = _wiki_fallback(pick["ent"], plaus)
    if wb: return wb
    return dict(status="no_birthday", detail=f"{pick['qid']} ({why}) has no exact birth date", year=pick["by"])

def _wiki_fallback(ent, plaus):
    t = ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")
    if not t: return None
    bd, title, _ = _wp_birth(t)
    if bd and plaus(int(bd[:4])): return dict(status="filled", birthday=bd, source=f"wikipedia {title}", detail=f"infobox of the Wikipedia page linked from {ent.get('id')}")
    return None

def apply(c, name, res):
    """Save a confident result: never overwrite a birth date or a hand edit; follows merged people."""
    k = db.fold(" ".join(name.split()))
    row = c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone(); hops = 0
    while row and row["merged_into"] and hops < 5:
        row = c.execute("SELECT * FROM people WHERE id=?", (row["merged_into"],)).fetchone(); hops += 1
    if row is None:
        c.execute("INSERT INTO people(name, name_norm, birthday, birth_date_source) VALUES(?,?,?,?)", (" ".join(name.split()), k, res["birthday"], res["source"]))
        return True
    if (row["birthday"] or "") or "birthday" in json.loads(row["edited_fields"] or "[]"): return False
    c.execute("UPDATE people SET birthday=?, birth_date_source=? WHERE id=? AND COALESCE(birthday,'')=''", (res["birthday"], res["source"], row["id"]))
    return True

def _record(c, name, res):
    c.execute("INSERT OR REPLACE INTO birthdate_lookup(name_norm, name, status, birthday, source, detail, at, fb_at) VALUES(?,?,?,?,?,?,?,?)",
              (db.fold(" ".join(name.split())), name, res["status"], res.get("birthday"), res.get("source"), res.get("detail"), time.time(),
               res.get("fb_at")))

def _hints(c, name, row):
    """TMDB person id, library film TMDB ids, credit years and adult flag for one name (all its aliases)."""
    names = db.expand_person(c, name); fk = {db.fold(n) for n in names}
    tmdb_person = row["tmdb_person_id"] if row is not None and (row["tmdb_person_id"] or 0) > 0 else None
    ids, years, adult = [], [], False
    like = " OR ".join(f"person_in_any(f.{col}, ?)" for col in db.PEOPLE_ROLES.values())
    for r in c.execute(f"SELECT f.tmdb_id, f.year, f.kind, f.primary_genre FROM films f WHERE {like}", [db.ALIAS_SEP.join(names)] * len(db.PEOPLE_ROLES)):
        if r["tmdb_id"]: ids.append(r["tmdb_id"])
        if r["year"]: years.append(r["year"])
        if r["kind"] == "scene" or (r["primary_genre"] or "") in ("Hardcore", "Softcore"): adult = True
    return tmdb_person, ids, years, adult

def tmdb_birthday(c, row):
    """TMDB person birthday for someone with a TMDB id (the profile may come from ThePornDB, which skips TMDB fields)."""
    if row is None or not TMDB["fn"] or (row["tmdb_person_id"] or 0) <= 0: return None
    key = db.get_setting(c, "tmdb_key", "") or ""
    if not key: return None
    try: p = TMDB["fn"](f"person/{row['tmdb_person_id']}", key)
    except Exception: return None
    bd = (p.get("birthday") or "")[:10]
    try: datetime.date.fromisoformat(bd); return dict(status="filled", birthday=bd, source=f"tmdb person {row['tmdb_person_id']}", detail="TMDB profile")
    except ValueError: return None

def _evidence(c, name, row):
    """What the fallback sites are checked against: every name, credit years (library, TMDB credits, ThePornDB scenes,
    profile career), titles, profile links to the sites, and whether the person acts at all (crew-only people are skipped)."""
    names = db.expand_person(c, name)
    if row is not None:
        try: names += [a for a in json.loads(row["aliases"] or "[]") if isinstance(a, str) and len(a.split()) >= 2 and a not in names]
        except Exception: pass
    years, titles, acting = set(), set(), False
    sel = " OR ".join(f"person_in_any(f.{col}, ?)" for col in db.PEOPLE_ROLES.values())
    for r in c.execute(f"SELECT f.title, f.year, person_in_any(f.cast_, ?) AS ac FROM films f WHERE {sel}",
                       [db.ALIAS_SEP.join(names)] * (len(db.PEOPLE_ROLES) + 1)):
        if r["year"]: years.add(int(r["year"]))
        if r["ac"]: acting = True; titles.add(r["title"] or "")
    urls = {}
    if row is not None:
        try: cr = json.loads(row["credits_json"] or "{}")
        except Exception: cr = {}
        for x in cr.get("cast") or []:
            acting = True; titles.add(x.get("title") or ""); titles.add(x.get("original_title") or "")
            if re.match(r"^\d{4}", x.get("release_date") or ""): years.add(int(x["release_date"][:4]))
        try: sc = json.loads(row["tpdb_scenes_json"] or "[]")
        except Exception: sc = []
        for x in sc if isinstance(sc, list) else []:
            acting = True
            if re.match(r"^\d{4}", str(x.get("date") or "")): years.add(int(str(x["date"])[:4]))
        for y in re.findall(r"\b(19\d\d|20\d\d)\b", row["career"] or ""): years.add(int(y))
        if row["source"] == "tpdb" or row["tpdb_performer_id"]: acting = True
        try: links = json.loads(row["links_json"] or "{}")
        except Exception: links = {}
        for k2, v in (links or {}).items():
            k2 = str(k2).lower().replace(" ", "")
            if k2 in bdsources.LABEL and isinstance(v, str) and v.startswith("https://"): urls[k2] = v
        if row["merged_into"] is None: acting = True  # has a person page (e.g. Dashboard 'People without a birth date')
    return dict(names=names, years=sorted(years), titles=sorted(t for t in titles if t), acting=acting, urls=urls)

def fallback(c, name, row, res0):
    """Try the fallback sites after TMDB / Wikidata / Wikipedia gave no exact date. Returns the new result."""
    ev = _evidence(c, name, row)
    if not ev["acting"]: return dict(res0, fb_at=time.time())
    wd_year = res0.get("year") if res0["status"] == "no_birthday" else None
    fb = bdsources.lookup(ev["names"], ev["years"], ev["titles"], wd_year, ev["urls"])
    note = (f"checked {', '.join(fb['tried'])}" if fb["tried"] else "") + \
           ("; skipped " + ", ".join(f"{k} ({v})" for k, v in fb["skipped"].items()) if fb["skipped"] else "")
    if not fb["tried"]: return dict(res0)  # every site is paused right now: no fb_at, so it is tried again later
    hit = fb["hit"]
    wd_cands = []
    if res0["status"] == "ambiguous":
        try: wd_cands = [x for x in json.loads(res0.get("detail") or "[]") if isinstance(x, dict)]
        except Exception: wd_cands = []
    if hit and any(x.get("birthday") and x["birthday"] != hit["birthday"] for x in wd_cands if x.get("qid")):
        fb["cands"].append(hit); hit = None  # a Wikidata candidate has a different exact date: let Kyle decide
    if hit:
        return dict(status="filled", birthday=hit["birthday"], source=f"{hit['site']} {hit['url']}",
                    detail=f"{hit['label']}: {hit['why']}", fb_at=time.time(), site=hit["site"])
    if fb["cands"]:
        return dict(status="ambiguous", fb_at=time.time(), year=res0.get("year"),
                    detail=json.dumps(wd_cands + [dict(site=x["site"], label=x["label"], url=x["url"], birthday=x["birthday"],
                                                       desc=f"{x['name']} on {x['label']}" + (f", career {x['career']}" if x.get("career") else ""),
                                                       why=x["why"]) for x in fb["cands"]][:8]))
    if res0["status"] == "ambiguous": return dict(res0, fb_at=time.time())
    return dict(res0, fb_at=time.time(), detail=((res0.get("detail") or "") + (f"; {note}: no usable page" if note else "")).strip("; "))

def lookup_one(c, name, recheck=False):
    k = db.fold(" ".join(name.split()))
    row = c.execute("SELECT * FROM people WHERE name_norm=?", (k,)).fetchone()
    if row is not None and ((row["birthday"] or "") or "birthday" in json.loads(row["edited_fields"] or "[]")): return dict(status="skipped")
    for n in db.expand_person(c, name)[1:]:  # an alias already has a birth date
        r2 = c.execute("SELECT birthday FROM people WHERE name_norm=?", (db.fold(n),)).fetchone()
        if r2 and r2["birthday"]: return dict(status="skipped", detail="alias has a birth date")
    prev = c.execute("SELECT * FROM birthdate_lookup WHERE name_norm=?", (k,)).fetchone() if recheck else None
    if prev is not None and prev["status"] in FB_RETRY:  # tried before the fallback sites existed: only try those
        res = dict(status=prev["status"], detail=prev["detail"])
    else:
        res = tmdb_birthday(c, row)
        if not res:
            tp, ids, years, adult = _hints(c, name, row)
            res = lookup(name, tp, ids, years, adult)
    if res["status"] in FB_RETRY and (FB_FORCE[0] or enabled()): res = fallback(c, name, row, res)  # never online for test DBs
    if res["status"] == "filled" and not apply(c, name, res): res = dict(res, status="skipped")
    _record(c, name, res); c.commit()
    return res

def queue(c, with_recheck=False):
    """Everyone credited in the library without a birth date and not tried yet, plus (with_recheck) people tried before the
    fallback sites existed. People with a person page first (Dashboard 'People without a birth date'), then adult
    performers, then most credits."""
    from collections import Counter
    have = {r[0] for r in c.execute("SELECT name_norm FROM people WHERE COALESCE(birthday,'')<>'' OR edited_fields LIKE '%birthday%'")}
    merged = {r[0] for r in c.execute("SELECT name_norm FROM people WHERE merged_into IS NOT NULL")}
    recheck = {r[0] for r in c.execute(f"SELECT name_norm FROM birthdate_lookup WHERE fb_at IS NULL AND status IN ({','.join('?' * len(FB_RETRY))})",
                                       FB_RETRY)} if with_recheck else set()
    tried = {r[0] for r in c.execute("SELECT name_norm FROM birthdate_lookup WHERE status<>'error' OR at>?", (time.time() - 86400,))} - recheck
    page = {r[0]: r[1] for r in c.execute("SELECT name_norm, name FROM people WHERE merged_into IS NULL AND COALESCE(birthday,'')='' "
                                          "AND COALESCE(edited_fields,'') NOT LIKE '%birthday%'")}
    groups = db.alias_groups(c)
    cnt, adult, disp = Counter(), Counter(), {}
    cols = list(db.PEOPLE_ROLES.values())
    for r in c.execute(f"SELECT kind, primary_genre, {', '.join(cols)} FROM films"):
        a = r["kind"] == "scene" or (r["primary_genre"] or "") in ("Hardcore", "Softcore")
        seen = set()
        for col in cols:
            for n in (r[col] or "").split(","):
                n = " ".join(n.split()); k = db.fold(n)
                if not n or k in seen: continue
                seen.add(k); cnt[k] += 1; disp.setdefault(k, n)
                if a and col == "cast_": adult[k] += 1
    for k, n in page.items(): disp.setdefault(k, n)
    out = []
    for k in set(cnt) | set(page):
        if k in have or k in merged or k in tried: continue
        if any(db.fold(x) in have for x in groups.get(k, [])): continue
        out.append((k not in page, -adult[k], -cnt[k], k))
    out.sort()
    return [(disp[k], k in recheck) for *_, k in out] if with_recheck else [disp[k] for *_, k in out]

def run_job(connect, limit=None):
    if JOB["running"]: return False
    JOB.update(running=True, stop=False, done=0, filled=0, ambiguous=0, not_found=0, no_birthday=0, errors=0, current=None,
               started_at=time.time(), finished_at=None, last_error=None, by_source={}, rechecked=0)
    def work():
        c = connect(); ensure(c)
        try:
            q = queue(c, with_recheck=True)
            if limit: q = q[:limit]
            JOB["total"] = len(q)
            for name, again in q:
                while PRIO["running"] and not JOB["stop"]:  # queued films' cast first (same per-site rate limits)
                    JOB["current"] = "(waiting: queued films first)"; time.sleep(1)
                if JOB["stop"]: break
                if db.fold(" ".join(name.split())) in PRIO_DONE: JOB["done"] += 1; continue  # already looked up for a queued film
                JOB["current"] = name
                try:
                    res = lookup_one(c, name, recheck=again); st = res["status"]
                    if st in JOB: JOB[st] += 1
                    if again: JOB["rechecked"] += 1
                    if st == "filled":
                        src = (res.get("source") or "?").split(" ")[0]; JOB["by_source"][src] = JOB["by_source"].get(src, 0) + 1
                except Exception as ex:
                    JOB["errors"] += 1; JOB["last_error"] = f"{name}: {type(ex).__name__}"
                    try: _record(c, name, dict(status="error", detail=type(ex).__name__ + ": " + str(ex)[:120])); c.commit()
                    except Exception: pass
                    if JOB["errors"] > 50 and JOB["errors"] > JOB["done"] / 2: JOB["last_error"] += " (stopped: too many errors)"; break
                JOB["done"] += 1
        finally:
            JOB.update(running=False, current=None, finished_at=time.time()); c.close()
    threading.Thread(target=work, daemon=True).start()
    return True

PRIO = dict(running=False, todo=[], done=0, total=0, filled=0, review=0, current=None, started_at=None, finished_at=None, results=[])
PRIO_LOCK = threading.Lock()
PRIO_DONE = set()  # name keys looked up for queued films this session (the background job skips them)
PRIO_SKIP = ("ambiguous", "reviewed_none", "skipped", "filled")  # already in review / decided: not looked up again

def queued_film_ids(c):
    return [r[0] for r in c.execute("SELECT film_id FROM smart_queue ORDER BY added_at")]

RETRY_DAYS = 7  # not found / no exact date: tried again for queued films once the last try is this old

def queue_todo(c, film_ids, retry=False):
    """[(name, mode)] to look up for these films' cast: never tried ('full'), tried before the fallback sites existed
    ('recheck'), errors, not found / no exact date last tried more than RETRY_DAYS ago, and with retry=True every earlier
    not found / no exact date ('full' again). Scene cast entries that are really a title ('Kelsey Kane Cum Inside Me')
    are skipped: no site knows them (fix the cast; export-queue lists them as 'title_as_name')."""
    ensure(c)
    out, seen = [], set()
    old = time.time() - RETRY_DAYS * 86400
    for fid in film_ids:
        r = c.execute("SELECT cast_, kind FROM films WHERE id=?", (fid,)).fetchone()
        for m in db.cast_missing_birthdates(c, r["cast_"] if r else "", scene=bool(r) and r["kind"] == "scene"):
            k = db.fold(m["name"])
            if k in seen or m["status"] in PRIO_SKIP or m["status"] in ("hand_edited", "title_as_name"): continue
            seen.add(k)
            lk = c.execute("SELECT status, fb_at, at FROM birthdate_lookup WHERE name_norm=?", (k,)).fetchone()
            if lk is None or lk["status"] == "error": out.append((m["name"], "full"))
            elif lk["status"] in FB_RETRY and lk["fb_at"] is None: out.append((m["name"], "recheck"))
            elif retry or (lk["status"] in ("not_found", "no_birthday") and (lk["at"] or 0) < old): out.append((m["name"], "full"))
    return out

def queue_lookup(connect, film_ids=None, retry=False, sync=False, auto=False):
    """Look up birth dates for the cast of smart-tag-queued films (default: the whole queue) ahead of the background job.
    Uses lookup_one, so the same sources, rate limits, review rules and hand-edit protection apply.
    auto=True (a film was just queued): only when online lookups are on ('run automatically'). -> number of names added."""
    c = connect()
    try:
        if auto and not (enabled() and db.get_setting(c, "birthdate_auto", True)): return 0
        todo = queue_todo(c, film_ids if film_ids is not None else queued_film_ids(c), retry)
    finally: c.close()
    with PRIO_LOCK:
        have = {db.fold(n) for n, _ in PRIO["todo"]} | ({db.fold(PRIO["current"])} if PRIO["current"] else set())
        new = [x for x in todo if db.fold(x[0]) not in have]
        if not new: return 0
        if not PRIO["running"]:
            PRIO.update(running=True, todo=[], done=0, total=0, filled=0, review=0, current=None, started_at=time.time(), finished_at=None)
            start = True
        else: start = False
        PRIO["todo"] += new; PRIO["total"] += len(new)
    if start:
        if sync: _prio_work(connect)
        else: threading.Thread(target=_prio_work, args=(connect,), daemon=True).start()
    return len(new)

def _prio_work(connect):
    c = connect(); ensure(c)
    try:
        while True:
            with PRIO_LOCK:
                if not PRIO["todo"]: PRIO.update(running=False, current=None, finished_at=time.time()); return
                name, mode = PRIO["todo"].pop(0); PRIO["current"] = name
            try:
                res = lookup_one(c, name, recheck=mode == "recheck")
            except Exception as ex:
                res = dict(status="error", detail=type(ex).__name__ + ": " + str(ex)[:120])
                try: _record(c, name, res); c.commit()
                except Exception: pass
            PRIO_DONE.add(db.fold(" ".join(name.split())))
            st = res.get("status")
            PRIO["done"] += 1; PRIO["filled"] += st == "filled"; PRIO["review"] += st in ("ambiguous", "not_found", "no_birthday", "error")
            PRIO["results"] = (PRIO["results"] + [dict(name=name, status=st, birthday=res.get("birthday") if st == "filled" else None,
                                                       source=res.get("source") if st == "filled" else None, at=time.time())])[-200:]
    finally:
        with PRIO_LOCK: PRIO.update(running=False, current=None, finished_at=PRIO["finished_at"] or time.time())
        c.close()

def enabled():
    """Off for throwaway databases in the temp folder (tests) so they never go online."""
    import os, tempfile
    return not os.path.abspath(db.DB_PATH).startswith(os.path.abspath(tempfile.gettempdir()))

PAGE = set()
def lookup_async(connect, name):
    """Single lookup when a person page loads (new people), in the background; the next page load shows it."""
    k = db.fold(" ".join((name or "").split()))
    if not k or k in PAGE or not enabled(): return
    c0 = connect()
    try:
        if not db.get_setting(c0, "birthdate_auto", True): return
    finally: c0.close()
    PAGE.add(k)
    def work():
        c = connect(); ensure(c)
        try:
            r0 = c.execute("SELECT status, fb_at FROM birthdate_lookup WHERE name_norm=? AND status<>'error'", (k,)).fetchone()
            if r0 and not (r0["status"] in FB_RETRY and r0["fb_at"] is None): return
            lookup_one(c, name, recheck=True)
        except Exception: pass
        finally: c.close()
    threading.Thread(target=work, daemon=True).start()

def init(app, connect, tmdb):
    from flask import request, jsonify
    TMDB["fn"] = tmdb
    c = connect(); ensure(c); c.close()

    def stats(c):
        st = {r[0]: r[1] for r in c.execute("SELECT status, COUNT(*) FROM birthdate_lookup GROUP BY status")}
        bysrc = {}
        for (s0,) in c.execute("SELECT source FROM birthdate_lookup WHERE status='filled'"):
            k0 = (s0 or "?").split(" ")[0]; bysrc[k0] = bysrc.get(k0, 0) + 1
        recheck = c.execute(f"SELECT COUNT(*) FROM birthdate_lookup WHERE fb_at IS NULL AND status IN ({','.join('?' * len(FB_RETRY))})", FB_RETRY).fetchone()[0]
        return dict(st, saved=c.execute("SELECT COUNT(*) FROM people WHERE birth_date_source IS NOT NULL").fetchone()[0],
                    by_source=bysrc, recheck_left=recheck)

    @app.get("/api/birthdates/status")
    def bd_status():
        c = connect(); ensure(c)
        return jsonify(job={k: v for k, v in JOB.items() if k != "stop"}, totals=stats(c), auto=bool(db.get_setting(c, "birthdate_auto", True)),
                       queued={k: v for k, v in PRIO.items() if k != "todo"} | dict(left=len(PRIO["todo"])),
                       sites=bdsources.status(), skipped_sites=bdsources.SKIPPED)

    @app.post("/api/birthdates/start")
    def bd_start():
        c = connect(); ensure(c)
        if (request.get_json(silent=True) or {}).get("auto") is not None: db.set_setting(c, "birthdate_auto", bool(request.get_json()["auto"]))
        lim = (request.get_json(silent=True) or {}).get("limit")
        return jsonify(started=run_job(connect, int(lim) if lim else None))

    @app.post("/api/birthdates/queue-lookup")
    def bd_queue_lookup():
        """'Look up birth dates now' for smart-tag-queued films. Body: {film_ids?: [..] (default: whole queue), retry?: true
        (also re-try people not found before; default true here)}. Runs ahead of the background job."""
        d = request.get_json(silent=True) or {}
        if not enabled(): return jsonify(added=0, note="offline (test database)")
        n = queue_lookup(connect, d.get("film_ids"), retry=bool(d.get("retry", True)))
        return jsonify(added=n, running=PRIO["running"], left=len(PRIO["todo"]), db_path=db.DB_PATH)

    @app.post("/api/birthdates/stop")
    def bd_stop():
        JOB["stop"] = True; return jsonify(ok=True)

    @app.get("/api/birthdates/review")
    def bd_review():
        c = connect(); ensure(c)
        out = []
        qd = {}  # name key -> titles of queued films crediting them as cast without a birth date
        for fid in queued_film_ids(c):
            f = c.execute("SELECT title, year, cast_ FROM films WHERE id=?", (fid,)).fetchone()
            if not f: continue
            for m in db.cast_missing_birthdates(c, f["cast_"]): qd.setdefault(db.fold(m["name"]), []).append(f"{f['title']} ({f['year'] or '?'})")
        rows = list(c.execute("SELECT name_norm, name, detail, at, status FROM birthdate_lookup WHERE status='ambiguous' ORDER BY name COLLATE NOCASE LIMIT 500"))
        if qd:
            ks = list(qd)
            rows = [r for r in c.execute(f"SELECT name_norm, name, detail, at, status FROM birthdate_lookup WHERE name_norm IN ({','.join('?' * len(ks))}) "
                                         "AND status IN ('ambiguous','not_found','no_birthday','error') ORDER BY name COLLATE NOCASE", ks)] + \
                   [r for r in rows if r["name_norm"] not in qd]
        for r in rows:
            try: cands = json.loads(r["detail"] or "[]")
            except Exception: cands = []
            p = c.execute("SELECT id, birthday, edited_fields, merged_into FROM people WHERE name_norm=?", (r["name_norm"],)).fetchone()
            if p and p["merged_into"]: p = c.execute("SELECT id, birthday, edited_fields, merged_into FROM people WHERE id=?", (p["merged_into"],)).fetchone() or p
            if p and ((p["birthday"] or "") or "birthday" in json.loads(p["edited_fields"] or "[]")): continue  # settled since (TMDB, hand edit, merge)
            x = dict(key=r["name_norm"], name=r["name"], pid=p["id"] if p else None, candidates=cands if isinstance(cands, list) else [],
                     iafd_search="https://www.iafd.com/results.asp?searchtype=comprehensive&searchstring=" + urllib.parse.quote(r["name"] or ""))
            if r["name_norm"] in qd:
                x.update(queued=qd[r["name_norm"]], status=r["status"])
                if r["status"] != "ambiguous":  # nothing usable found: links to check by hand
                    x.update(note=(r["detail"] or "")[:200], links=[dict(label="Wikipedia", url="https://en.wikipedia.org/w/index.php?search=" + urllib.parse.quote(r["name"] or ""))] +
                             [dict(label=st.label, url=st.url_for(r["name"])) for st, _ in bdsources.SITES])
            out.append(x)
        return jsonify(review=out, queued_count=sum(1 for x in out if x.get("queued")))

    @app.post("/api/birthdates/review")
    def bd_review_pick():
        """Kyle picks the right Wikidata person (or none). Saves only a day-precision P569 and never over a hand edit."""
        d = request.get_json(silent=True) or {}; c = connect(); ensure(c)
        r = c.execute("SELECT * FROM birthdate_lookup WHERE name_norm=?", (d.get("key") or "",)).fetchone()
        if not r: return jsonify(error="Unknown entry"), 404
        if d.get("url"):  # a fallback-site candidate: the date read from that page when it was checked
            try: cands = json.loads(r["detail"] or "[]")
            except Exception: cands = []
            x = next((x for x in cands if isinstance(x, dict) and x.get("url") == d["url"] and x.get("site") in bdsources.LABEL), None)
            if not x or not x.get("birthday"): return jsonify(error="Unknown candidate"), 400
            res = dict(status="filled", birthday=x["birthday"], source=f"{x['site']} {x['url']}", detail=f"picked in review ({x['label']})", fb_at=time.time())
            ok = apply(c, r["name"], res); _record(c, r["name"], res if ok else dict(res, status="skipped")); c.commit()
            return jsonify(ok=ok, birthday=x["birthday"])
        if not d.get("qid"):
            _record(c, r["name"], dict(status="reviewed_none", detail="picked none in review", fb_at=time.time())); c.commit(); return jsonify(ok=True)
        q = str(d["qid"])
        if not re.match(r"^Q\d+$", q): return jsonify(error="bad id"), 400
        ent = _entities([q]).get(q) or {}; bd, _ = _dob(ent)
        if not bd: return jsonify(error=f"{q} has no exact birth date"), 400
        res = dict(status="filled", birthday=bd, source=f"wikidata {q}", detail="picked in review")
        ok = apply(c, r["name"], res); _record(c, r["name"], res if ok else dict(res, status="skipped")); c.commit()
        return jsonify(ok=ok, birthday=bd)

    def auto():
        time.sleep(90)
        c = connect()
        try: on = bool(db.get_setting(c, "birthdate_auto", True))
        finally: c.close()
        if on: run_job(connect)
    if enabled(): threading.Thread(target=auto, daemon=True).start()
