"""Reports: read-only statistics over the user's own library (films, scenes, tags, credits).

Everything here only reads the database. Reports are described by a small whitelisted *spec* (a dict), so the
'Ask a question' box can let an AI pick a report type and filters without ever writing SQL:

  {"report": "country" | "directors" | "tag_decades" | "genres" | "studios" | "performers",
   "genre": "Horror", "tag": "Exploitation", "decade": "1970s", "scope": "all" | "adult",
   "rank": "count" | "share", "limit": 25}

Tag synonym groups (setting 'report_synonyms', a JSON list of lists) are applied whenever a tag filter is used.
'Adult' films are films whose primary genre is Hardcore. Performer reports only include people with a known
birth date, only count titles released when they were adults, and never break performers down by tag."""
import re, json, datetime

REPORTS = {
    "country": "Films by country",
    "directors": "Top directors by tag",
    "tag_decades": "Tags over decades",
    "genres": "Primary genres",
    "studios": "Studios (scenes)",
    "performers": "Performers",
}
DEFAULT_SYNONYMS = [["Blowjob", "Fellatio", "Oral Sex"]]
ADULT_GENRE = "Hardcore"
DEC_RE = re.compile(r"^(1[89]|20)\d0s$")


def _split(s): return [x.strip() for x in (s or "").split(",") if x.strip()]
def _dec(y): return f"{y // 10 * 10}s" if y else None


def synonyms(c):
    import db
    v = db.get_setting(c, "report_synonyms", None)
    if not isinstance(v, list): v = DEFAULT_SYNONYMS
    return [[str(x).strip() for x in g if str(x).strip()] for g in v if isinstance(g, list)]


def set_synonyms(c, groups):
    import db
    if not isinstance(groups, list) or not all(isinstance(g, list) and all(isinstance(x, str) for x in g) for g in groups):
        raise ValueError("Synonyms must be a list of lists of tag names, e.g. [[\"Blowjob\", \"Fellatio\"]]")
    groups = [[x.strip() for x in g if x.strip()] for g in groups]
    groups = [g for g in groups if len(g) >= 2]
    db.set_setting(c, "report_synonyms", groups); c.commit()
    return groups


def tag_group(c, tag):
    """The tag plus its synonyms (case-insensitive), keeping the first spelling seen."""
    t = (tag or "").strip()
    if not t: return []
    for g in synonyms(c):
        if t.lower() in (x.lower() for x in g):
            return g
    return [t]


def tagged_ids(c, names):
    """Film ids carrying any of these tags (approved), via the tag-name and film_tags(tag_id) indexes."""
    if not names: return None
    q = ",".join("?" * len(names))
    return {r[0] for r in c.execute(
        f"SELECT ft.film_id FROM tags t JOIN film_tags ft ON ft.tag_id=t.id WHERE ft.status='approved' AND t.name IN ({q}) COLLATE NOCASE",
        names)}


def _films(c):
    out = {}
    for i, t, y, rd, d, co, st, pg, k in c.execute(
            "SELECT id,title,year,release_date,director,country,studio,primary_genre,kind FROM films"):
        if not y and rd and rd[:4].isdigit(): y = int(rd[:4])
        out[i] = dict(id=i, t=t, y=y, rd=rd, dir=_split(d), co=_split(co), st=(st or "").strip(), pg=pg or "",
                      scene=(k == "scene"))
    return out


def _rank(groups, limit, denom=None, rank="count"):
    rows = []
    for k, ids in groups.items():
        r = dict(k=k, n=len(ids), ids=sorted(ids))
        if denom is not None:
            r["tot"] = denom.get(k, 0); r["pct"] = round(100 * len(ids) / r["tot"], 1) if r["tot"] else 0
        rows.append(r)
    key = (lambda r: (-r["pct"], -r["n"], r["k"].lower())) if rank == "share" and denom is not None else (lambda r: (-r["n"], r["k"].lower()))
    rows.sort(key=key)
    return rows[:limit]


def clean_spec(c, spec):
    """Validate a (possibly AI-produced) spec against the whitelist. Returns (spec, problems)."""
    spec = spec if isinstance(spec, dict) else {}
    out, probs = {}, []
    r = str(spec.get("report") or "").strip()
    if r not in REPORTS: probs.append(f"unknown report '{r}'"); r = "country"
    out["report"] = r
    g = str(spec.get("genre") or "").strip()
    if g:
        row = c.execute("SELECT primary_genre FROM films WHERE primary_genre=? COLLATE NOCASE LIMIT 1", (g,)).fetchone()
        if row: out["genre"] = row[0]
        else: probs.append(f"no primary genre called '{g}'")
    t = str(spec.get("tag") or "").strip()
    if t:
        names = tag_group(c, t)
        q = ",".join("?" * len(names))
        row = c.execute(f"SELECT name FROM tags WHERE name IN ({q}) COLLATE NOCASE LIMIT 1", names).fetchone()
        if row: out["tag"] = t if t.lower() in (n.lower() for n in names) and len(names) > 1 else row[0]
        else: probs.append(f"no tag called '{t}'")
    d = str(spec.get("decade") or "").strip().lower()
    if d:
        if re.match(r"^\d{4}$", d): d += "s"
        if re.match(r"^\d0s$", d): d = ("19" if int(d[0]) >= 2 else "20") + d
        if DEC_RE.match(d): out["decade"] = d
        else: probs.append(f"bad decade '{d}'")
    out["scope"] = "adult" if spec.get("scope") == "adult" else "all"
    out["rank"] = "share" if spec.get("rank") == "share" else "count"
    try: out["limit"] = max(1, min(100, int(spec.get("limit") or 25)))
    except (TypeError, ValueError): out["limit"] = 25
    return out, probs


def describe(s):
    """Plain-English reading of a cleaned spec (built by us, never taken from the AI)."""
    r = s["report"]; bits = []
    if r == "country":
        x = "films"
        if s.get("genre"): x = f"{s['genre']} films"
        if s.get("tag"): x += f" tagged {s['tag']}"
        if s.get("decade"): x += f" from the {s['decade']}"
        return f"countries ranked by number of {x}"
    if r == "directors":
        x = "adult films (primary genre Hardcore)" if s["scope"] == "adult" else "films"
        if s.get("decade"): x = f"{s['decade']} {x}"
        if s.get("tag"): x += f" tagged {s['tag']}"
        return f"directors ranked by {'share of their films' if s['rank'] == 'share' else 'number'} of {x}"
    if r == "tag_decades": return f"share of each decade's titles tagged {s.get('tag', '?')}"
    if r == "genres": return "primary genres ranked by number of films" + (f" from the {s['decade']}" if s.get("decade") else "")
    if r == "studios":
        x = "studios ranked by number of scenes" + (f" from the {s['decade']}" if s.get("decade") else "")
        return x + (f" tagged {s['tag']}" if s.get("tag") else "")
    if r == "performers": return "performers with a known birth date, ranked by titles released when they were adults"
    return r


def run(c, spec):
    s, probs = clean_spec(c, spec)
    F = _films(c)
    names = tag_group(c, s.get("tag")) if s.get("tag") else []
    tag_ids = tagged_ids(c, names) if names else None
    notes = []
    if len(names) > 1: notes.append("Tag synonyms applied: " + ", ".join(names) + " count as the same tag.")
    dec = s.get("decade"); r = s["report"]; lim = s["limit"]
    res = dict(spec=s, title=REPORTS[r], reading=describe(s), problems=probs, notes=notes, rows=[])
    if r == "country":
        gr = {}; base = 0; nocountry = 0
        for f in F.values():
            if f["scene"] or (s.get("genre") and f["pg"].lower() != s["genre"].lower()) or (tag_ids is not None and f["id"] not in tag_ids) or (dec and _dec(f["y"]) != dec): continue
            base += 1
            if not f["co"]: nocountry += 1
            for co in f["co"]: gr.setdefault(co, set()).add(f["id"])
        res["rows"] = _rank(gr, lim); res["base"] = base
        for row in res["rows"]: row["pct"] = round(100 * row["n"] / base, 1) if base else 0; row["tot"] = base
        notes.append(f"{base} matching film(s); a film with several production countries counts for each" + (f"; {nocountry} have no country." if nocountry else "."))
    elif r == "directors":
        pool = [f for f in F.values() if not f["scene"] and f["dir"] and (not dec or _dec(f["y"]) == dec) and (s["scope"] == "all" or f["pg"] == ADULT_GENRE)]
        tot = {}
        for f in pool:
            for x in f["dir"]: tot[x] = tot.get(x, 0) + 1
        gr = {}
        for f in pool:
            if tag_ids is None or f["id"] in tag_ids:
                for x in f["dir"]: gr.setdefault(x, set()).add(f["id"])
        res["rows"] = _rank(gr, lim, tot, s["rank"])
        res["base"] = sum(1 for f in pool if tag_ids is None or f["id"] in tag_ids)
        notes.append(f"{res['base']} of {len(pool)} {dec + ' ' if dec else ''}{'adult ' if s['scope'] == 'adult' else ''}films with a director match. Share = tagged films ÷ that director's films in the same set.")
        if s["scope"] == "adult": notes.append("Adult films = primary genre Hardcore.")
    elif r == "tag_decades":
        if tag_ids is None: res["problems"].append("choose a tag"); tag_ids = set()
        decs = sorted({_dec(f["y"]) for f in F.values() if f["y"] and f["y"] >= 1900})
        tot = {d: 0 for d in decs}; gr = {d: set() for d in decs}
        for f in F.values():
            d = _dec(f["y"]) if f["y"] and f["y"] >= 1900 else None
            if not d: continue
            tot[d] += 1
            if f["id"] in tag_ids: gr[d].add(f["id"])
        res["rows"] = [dict(k=d, n=len(gr[d]), tot=tot[d], pct=round(100 * len(gr[d]) / tot[d], 1) if tot[d] else 0, ids=sorted(gr[d])) for d in decs]
        res["chart"] = "columns"
    elif r == "genres":
        gr = {}
        for f in F.values():
            if not f["scene"] and f["pg"] and (not dec or _dec(f["y"]) == dec) and (tag_ids is None or f["id"] in tag_ids):
                gr.setdefault(f["pg"], set()).add(f["id"])
        res["rows"] = _rank(gr, lim)
    elif r == "studios":
        gr = {}
        for f in F.values():
            if f["scene"] and f["st"] and (not dec or _dec(f["y"]) == dec) and (tag_ids is None or f["id"] in tag_ids):
                gr.setdefault(f["st"], set()).add(f["id"])
        res["rows"] = _rank(gr, lim)
    elif r == "performers":
        if names: notes.append("Tag filters are never applied to performers; counts only.")
        try:
            res["rows"] = performers(c, F, dec, lim)
            known = c.execute("SELECT COUNT(*) FROM people WHERE merged_into IS NULL AND birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'").fetchone()[0]
        except Exception: res["rows"], known = [], 0  # no people table yet
        notes.append(f"Only the {known} people with a known birth date are included, counting only titles released when they were adults (18+). Everyone else is left out.")
    return res


def performers(c, F, dec, lim):
    people = {}
    for n, b in c.execute("SELECT name,birthday FROM people WHERE merged_into IS NULL AND birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"):
        try: people[n.lower()] = (n, datetime.date.fromisoformat(b))
        except ValueError: pass
    if not people: return []
    gr = {}
    for f in F.values():
        if dec and _dec(f["y"]) != dec: continue
        for nm in _split_cast(c, f["id"]):
            p = people.get(nm.lower())
            if not p: continue
            n, bd = p
            adult_from = bd.replace(year=bd.year + 18) if not (bd.month == 2 and bd.day == 29) else datetime.date(bd.year + 18, 3, 1)
            rel = None
            if f["rd"]:
                try: rel = datetime.date.fromisoformat(f["rd"][:10])
                except ValueError: rel = None
            ok = (rel >= adult_from) if rel else (f["y"] is not None and f["y"] >= bd.year + 19)  # year only: be conservative
            if ok: gr.setdefault(n, set()).add(f["id"])
    return _rank(gr, lim)


_CAST = {}
def _split_cast(c, fid):
    if not _CAST.get("_c") is c:
        _CAST.clear(); _CAST["_c"] = c
        for i, ca in c.execute("SELECT id,cast_ FROM films"): _CAST[i] = _split(ca)
    return _CAST.get(fid, [])


def options(c):
    genres = [r[0] for r in c.execute("SELECT primary_genre,COUNT(*) n FROM films WHERE primary_genre IS NOT NULL AND primary_genre!='' AND (kind IS NULL OR kind!='scene') GROUP BY 1 ORDER BY n DESC")]
    years = [r[0] for r in c.execute("SELECT DISTINCT year/10*10 FROM films WHERE year>=1900 ORDER BY 1")]
    tags = [r[0] for r in c.execute("SELECT t.name FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved' GROUP BY t.id ORDER BY COUNT(*) DESC LIMIT 300")]
    return dict(reports=REPORTS, genres=genres, decades=[f"{y}s" for y in years], tags=tags, synonyms=synonyms(c))


def films_list(c, ids):
    ids = [int(i) for i in ids if str(i).isdigit()][:2000]
    if not ids: return []
    q = ",".join("?" * len(ids))
    rows = c.execute(f"SELECT id,title,year,kind,studio,director FROM films WHERE id IN ({q})", ids).fetchall()
    rows.sort(key=lambda r: (r[2] or 0, (r[1] or "").lower()))
    return [dict(id=r[0], title=r[1], year=r[2], scene=r[3] == "scene", studio=r[4], director=r[5]) for r in rows]


# ---------- Ask a question (AI picks a whitelisted spec; never SQL) ----------
def ask_prompt(c, question):
    o = options(c)
    ql = question.lower()
    words = set(re.findall(r"[a-z0-9][a-z0-9'\-]+", ql))
    hits = []
    for (name,) in c.execute("SELECT name FROM tags"):
        nl = name.lower()
        if nl in ql or (nl in words):
            hits.append(name)
    hits = sorted(set(hits), key=lambda x: -len(x))[:40]
    syn = [g for g in o["synonyms"]]
    return (
        "You turn a question about a personal film library into ONE JSON object choosing a report. Reply with JSON only.\n"
        "Allowed keys and values:\n"
        ' "report": one of ' + json.dumps(list(REPORTS)) + "\n"
        "   country = countries ranked by number of films (optionally filtered by genre/tag/decade)\n"
        "   directors = directors ranked by films with a tag (optionally decade, scope, rank)\n"
        "   tag_decades = how common a tag is in each decade\n"
        "   genres = primary genres ranked (optional decade/tag)\n"
        "   studios = scene studios ranked (optional decade/tag)\n"
        "   performers = performers ranked by number of titles (no tags)\n"
        ' "genre": a primary genre from ' + json.dumps(o["genres"][:60]) + "\n"
        ' "tag": a tag name exactly as listed. Tags whose names appear in the question: ' + json.dumps(hits) +
        ". Common tags: " + json.dumps(o["tags"][:80]) + ". Synonym groups (any member is fine): " + json.dumps(syn) + "\n"
        ' "decade": like "1970s" from ' + json.dumps(o["decades"]) + "\n"
        ' "scope": "adult" when the question is about porn/adult/hardcore films (directors report), else "all"\n'
        ' "rank": "share" if the question asks about proportion/percentage, else "count"\n'
        ' "limit": number of rows, default 25\n'
        "Note: 'exploitation' means the tag Exploitation, not a genre. Omit keys you do not need.\n"
        "Question: " + question.strip()[:500])


def parse_spec(text):
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t.strip())
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i: raise ValueError("The AI reply had no JSON object")
    v = json.loads(t[i:j + 1])
    if not isinstance(v, dict): raise ValueError("The AI reply was not a JSON object")
    return {k: v[k] for k in ("report", "genre", "tag", "decade", "scope", "rank", "limit") if k in v}


# ---------- Ask a question, free form: the AI writes ONE read-only SELECT ----------
# Safety: separate read-only connection (URI mode=ro + PRAGMA query_only), one SELECT/WITH statement only, a
# keyword blacklist, a ~10 s progress-handler timeout and a 200-row cap. Performer data is only reachable through
# the TEMP table film_cast, which holds only (film, person) pairs for people with a known birth date on titles
# released when they were adults; TEMP 'films' and 'people' shadow the real tables so cast_ cannot be read directly.
import sqlite3, time
SQL_MAX_ROWS, SQL_TIMEOUT = 200, 10.0
_BAD = re.compile(r"\b(attach|detach|pragma|insert|update|delete|drop|create|alter|vacuum|reindex|analyze|"
                  r"load_extension|readfile|writefile|main|sqlite_master|sqlite_schema|sqlite_temp_master|cast_|"
                  r"birthday|merged_into)\b", re.I)
_ADULT_ON = "(kind IS NULL OR kind!='scene') AND primary_genre='Hardcore'"


def _db_file(c):
    for r in c.execute("PRAGMA database_list"):
        if r[1] == "main": return r[2]


def _adult_ok(bd, rd, y):
    adult_from = bd.replace(year=bd.year + 18) if not (bd.month == 2 and bd.day == 29) else datetime.date(bd.year + 18, 3, 1)
    rel = None
    if rd:
        try: rel = datetime.date.fromisoformat(rd[:10])
        except ValueError: rel = None
    return (rel >= adult_from) if rel else (y is not None and y >= bd.year + 19)


def _ro_conn(c):
    """Read-only connection with pre-expanded TEMP tables; returns (conn, names of every cast member, safe names)."""
    path = _db_file(c)
    r = sqlite3.connect("file:" + path.replace("\\", "/").replace("?", "%3F") + "?mode=ro", uri=True, timeout=10)
    cols = [x[1] for x in r.execute("PRAGMA table_info(films)") if x[1] != "cast_"]
    r.execute("CREATE TEMP TABLE films AS SELECT " + ",".join('"%s"' % x for x in cols) + " FROM main.films")
    r.execute("CREATE TEMP TABLE film_directors(film_id INTEGER, director TEXT)")
    r.execute("CREATE TEMP TABLE film_cast(film_id INTEGER, person TEXT)")
    people = {}
    try:
        pcols = [x[1] for x in r.execute("PRAGMA main.table_info(people)") if x[1] not in ("birthday", "merged_into")]
        r.execute("CREATE TEMP TABLE people AS SELECT " + ",".join('"%s"' % x for x in pcols) + ", substr(birthday,1,4)+0 AS birth_year"
                  " FROM main.people WHERE merged_into IS NULL AND birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'")
        for n, b in r.execute("SELECT name,birthday FROM main.people WHERE merged_into IS NULL AND birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"):
            try: people[n.lower()] = (n, datetime.date.fromisoformat(b))
            except ValueError: pass
    except sqlite3.OperationalError:
        r.execute("CREATE TEMP TABLE people(name TEXT, birth_year INTEGER)")
    try:  # linked aliases (people.aliases / merges): credits under an alias count for the person with the birth date
        import db as _db
        for nm, bd in list(people.values()):
            for al in _db.expand_person(c, nm): people.setdefault(al.lower(), (nm, bd))
    except Exception: pass
    dirs, cast, everyone = [], set(), set()
    for fid, d, ca, rd, y in r.execute("SELECT id,director,cast_,release_date,year FROM main.films"):
        for x in _split(d): dirs.append((fid, x))
        for nm in _split(ca):
            everyone.add(nm.lower()); p = people.get(nm.lower())
            if p and _adult_ok(p[1], rd, y): cast.add((fid, p[0]))
    r.executemany("INSERT INTO film_directors VALUES(?,?)", dirs)
    r.executemany("INSERT INTO film_cast VALUES(?,?)", sorted(cast))
    r.execute("PRAGMA query_only=1")
    safe = {p for _, p in cast}
    safe = {k for k, (n, _) in people.items() if n in safe}  # canonical names and their linked aliases
    return r, everyone, safe


def sql_schema(c):
    tags = [t for t in options(c)["tags"][:60]]
    return (
        "SQLite tables (use ONLY these):\n"
        " films(id, title, year, release_date 'YYYY-MM-DD' or NULL, director (comma-separated), country, studio, primary_genre,"
        " runtime (minutes), rating (0-10 or NULL), watched (0/1),"
        " kind ('scene' for scenes, NULL for films))\n"
        " film_directors(film_id, director)  -- one row per film per director (already split)\n"
        " film_cast(film_id, person)  -- one row per film per performer (already split); use this for anything about performers/cast\n"
        " people(name, birth_year, ...)  -- performers/people\n"
        " tags(id, name, category); film_tags(film_id, tag_id, status) -- only count status='approved'\n"
        "Notes:\n"
        " - 'Classic Porn' / porn / adult films = films WHERE " + _ADULT_ON + ".\n"
        " - 'Exploitation', 'Giallo', etc. are usually TAGS (match tags.name case-insensitively), not genres. Common tags: " + json.dumps(tags) + "\n"
        " - Tag synonym groups (treat members as the same tag, use IN (...)): " + json.dumps(synonyms(c)) + "\n"
        " - 'Classic Porn' is NOT a tag: use the films condition above. Wrap OR conditions in parentheses: (a OR b) AND c.\n"
        " - Performers who appear WITH someone: self-join film_cast (fc2.film_id=fc1.film_id AND fc1.person LIKE '<them>') and"
        " leave them out (fc2.person NOT LIKE '<them>'), GROUP BY fc2.person.\n"
        " - Missing values: a column is empty when it IS NULL or = '' (e.g. no director: COALESCE(f.director,'')='').\n"
        " - Name columns after what they hold: year AS year (a single year is never a decade); only (year/10)*10 AS decade.\n"
        " - Decade: (year/10)*10. Compare names case-insensitively (LIKE or COLLATE NOCASE).\n"
        " - When each row is ONE film, include films.id AS id. When rows are groups (GROUP BY a person, country, decade...),"
        " do NOT select films.id; instead include group_concat(DISTINCT films.id) AS film_ids so the user can click through.\n"
        " - For 'which ... the most' questions return the top 10 ranked rows (ORDER BY count DESC LIMIT 10), not just 1.\n"
        " - Give columns readable aliases (e.g. director, films, decade).\n"
        "Examples:\n"
        " Q: Which director did Jane Doe work with the most? -> SELECT d.director, COUNT(DISTINCT d.film_id) AS films,"
        " group_concat(DISTINCT d.film_id) AS film_ids FROM film_directors d JOIN film_cast fc ON fc.film_id=d.film_id"
        " WHERE fc.person LIKE 'Jane Doe' GROUP BY d.director ORDER BY films DESC LIMIT 10\n"
        " Q: How many horror films per decade? -> SELECT (year/10)*10 AS decade, COUNT(*) AS films, group_concat(id) AS film_ids"
        " FROM films WHERE primary_genre LIKE 'Horror' AND year IS NOT NULL GROUP BY decade ORDER BY decade\n"
        " - Never add NOT IN / NOT EXISTS sub-queries that exclude the person's own films; at most add d.director NOT LIKE '<that person>'.\n"
        " - Percentages: put the part's condition inside SUM(CASE WHEN ... THEN 1 ELSE 0 END), NOT in WHERE (WHERE limits the total"
        " too). Always multiply by 100.0 first (integer division gives 0) and round: ROUND(100.0*part/total, 1) AS percent. Return"
        " the part, the total and the percent as three columns (e.g. matching, total, percent). Use NULLIF(total,0) to avoid /0.\n"
        " - Ratios: ROUND(1.0*a/NULLIF(b,0), 2) AS ratio, and also return a and b.\n"
        " - A question about ONE thing (one person, one tag...) needs ONE aggregate row: no GROUP BY.\n"
        " - 'How many films/titles ...' wants ONE number: SELECT COUNT(DISTINCT f.id) AS films ... with NO GROUP BY (never one row"
        " per film with a count of 1). Only GROUP BY when the question asks 'per X', 'by X' or 'each X'.\n"
        " - Share of a person's/genre's films that have a tag: never JOIN film_tags into the query (it drops untagged films from"
        " the total and counts films once per tag). Use EXISTS: SUM(CASE WHEN EXISTS (SELECT 1 FROM film_tags ft JOIN tags t ON"
        " t.id=ft.tag_id WHERE ft.film_id=f.id AND ft.status='approved' AND lower(t.name) IN (...)) THEN 1 ELSE 0 END) AS matching,"
        " COUNT(DISTINCT f.id) AS total.\n"
        " - Tags joined by 'or' ('tagged oral or fellatio') match films with ANY of them: lower(t.name) IN (...) including"
        " their synonym group members; tags joined by 'and' must all be present (one EXISTS per tag).\n"
        " Q: How many films did Jane Doe act in that are tagged comedy or satire? -> SELECT COUNT(DISTINCT f.id) AS films FROM films f"
        " JOIN film_cast fc ON fc.film_id=f.id JOIN film_tags ft ON ft.film_id=f.id AND ft.status='approved' JOIN tags t ON"
        " t.id=ft.tag_id WHERE fc.person LIKE 'Jane Doe' AND lower(t.name) IN ('comedy','satire')\n"
        " - 'between 1970 and 1990' = year BETWEEN 1970 AND 1990 (inclusive). Names in the question may be typed without accents;"
        " use the stored spelling given below when there is one.\n"
        " Q: What percent of Jane Doe's directed films are from the 1970s? -> SELECT SUM(CASE WHEN f.year BETWEEN 1970 AND 1979 THEN 1"
        " ELSE 0 END) AS matching, COUNT(DISTINCT f.id) AS total, ROUND(100.0*SUM(CASE WHEN f.year BETWEEN 1970 AND 1979 THEN 1 ELSE 0"
        " END)/NULLIF(COUNT(DISTINCT f.id),0), 1) AS percent, group_concat(DISTINCT CASE WHEN f.year BETWEEN 1970 AND 1979 THEN f.id END)"
        " AS film_ids FROM film_directors fd JOIN films f ON f.id=fd.film_id WHERE fd.director IN ('Jane Doe')\n")


def sql_prompt(c, question, hint=()):
    h = ""
    for x in hint:
        if len(x) == 2 and x[0] == "tags":  # ("tags", [(as asked, [stored names incl. synonyms])])
            for asked, stored in x[1]:
                h += f" - The tag '{asked}' means tags.name IN ({', '.join(chr(39) + n.replace(chr(39), chr(39) * 2) + chr(39) for n in stored)}).\n"
            continue
        if len(x) == 3 and x[0] == "director":  # ("director", name as asked, [stored spellings incl. aliases])
            lst = ", ".join("'" + n.replace("'", "''") + "'" for n in x[2])
            h += (f" - The director '{x[1]}' is stored in film_directors.director as {lst} (all the same person)."
                  f" Match with fd.director IN ({lst}).\n")
        else:
            a, b = x[-2], x[-1]
            h += f" - The performer '{a}' is stored in film_cast.person as exactly '{b}'.\n"
    return ("You answer questions about a personal film library by writing ONE SQLite SELECT query.\n" + sql_schema(c) + h +
            'Reply with JSON only: {"reading": "<one short line: how you understood the question>", "sql": "<one SELECT or WITH query, no semicolon>",'
            ' "chart": "<optional: ring, stat, bar, line, hbar, grouped, stacked, heatmap or table>"}\n'
            "Chart guide: ring = one row with part, total and percent; stat = one row of plain numbers; bar/line = per year, decade"
            " or month; hbar = a ranking of names; grouped = names with 2-3 numbers each; stacked/heatmap = counts by two categories.\n"
            "Limit results to at most 50 rows unless asked otherwise.\nQuestion: " + question.strip()[:500])


PARSED = {"chart": ""}  # the AI's optional chart hint from the last parsed reply (validated in askcharts.pick)


def _parse_sql_reply(text):
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", t)
    i, j = t.find("{"), t.rfind("}")
    v = None
    if i >= 0 and j > i:
        try: v = json.loads(t[i:j + 1])
        except ValueError: v = None
    if not isinstance(v, dict):
        m = re.search(r"((?:SELECT|WITH)\b.*)", t, re.S | re.I)
        if not m: raise ValueError("The AI reply had no query")
        v = {"sql": m.group(1), "reading": ""}
    PARSED["chart"] = str(v.get("chart") or "").strip()[:20]
    return str(v.get("reading") or "").strip(), str(v.get("sql") or "").strip()


def check_sql(sql):
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", s): raise ValueError("Only SELECT queries are allowed")
    if ";" in s: raise ValueError("Only one statement is allowed")
    m = _BAD.search(re.sub(r"'(?:[^']|'')*'", "''", s))
    if m: raise ValueError(f"'{m.group(0)}' is not allowed in the query")
    return s


_KIND_OR = re.compile(r"(?i)(?<!\()\s*\b((?:\w+\.)?kind\s+IS\s+NULL\s+OR\s+(?:\w+\.)?kind\s*(?:!=|<>)\s*'scene')(?!\s*\))")


def fix_sql(sql):
    """Deterministic repairs of common AI slips: 'kind IS NULL OR kind!='scene' AND ...' without parentheses (OR binds
    looser than AND, so every other condition is lost) -> '(kind IS NULL OR kind!='scene') AND ...'."""
    return _KIND_OR.sub(lambda m: " (" + m.group(1) + ")", sql)


def run_sql(r, sql, everyone, safe):
    s = check_sql(fix_sql(sql))
    t0 = time.monotonic()
    r.set_progress_handler(lambda: 1 if time.monotonic() - t0 > SQL_TIMEOUT else 0, 2000)
    try:
        cur = r.execute(s)
        cols = [d[0] for d in cur.description or []]
        raw = cur.fetchmany(SQL_MAX_ROWS + 1)
        if re.search(r"\bgroup\s+by\b", s, re.I) and "id" in cols:  # a bare id in a grouped row is an arbitrary film
            k = cols.index("id"); cols = cols[:k] + cols[k + 1:]; raw = [tuple(x[:k]) + tuple(x[k + 1:]) for x in raw]
    except sqlite3.OperationalError as e:
        if "interrupt" in str(e).lower(): raise ValueError("The query took too long (over 10 seconds)")
        raise
    finally: r.set_progress_handler(None, 0)
    unsafe = set()
    if re.search(r"\b(film_cast|people)\b", s, re.I):  # film_cast is already safe; this also catches names leaking via other columns
        dirs = {d.lower() for (d,) in r.execute("SELECT DISTINCT director FROM film_directors")}
        unsafe = everyone - safe - dirs
    rows, dropped = [], 0
    for row in raw[:SQL_MAX_ROWS]:  # age rule post-filter: no row may name a performer outside the safe list
        if any(isinstance(v, str) and any(p.strip().lower() in unsafe for p in v.split(",")) for v in row): dropped += 1; continue
        rows.append(list(row))
    return cols, rows, len(raw) > SQL_MAX_ROWS, dropped


def mentioned_people(c, question):
    """Performer and director names (as stored) that appear in the question, longest first, no overlaps. Matching is
    accent- and case-insensitive ('Jesus Franco' finds 'Jesús Franco'). Someone credited both ways counts as a director
    when the question is about directing, else as cast."""
    import db as _db
    ql = " " + re.sub(r"\s+", " ", _db.fold(question)) + " "
    names, roles = {}, {}
    for ca, d in c.execute("SELECT cast_, director FROM films"):
        for n in _split(ca): k = _db.fold(n); names.setdefault(k, n); roles.setdefault(k, set()).add("cast")
        for n in _split(d): k = _db.fold(n); names.setdefault(k, n); roles.setdefault(k, set()).add("director")
    try:  # linked aliases (people.aliases / merges) that no film credits directly
        for grp in _db.alias_groups(c).values():
            r0 = set().union(*[roles.get(_db.fold(x), set()) for x in grp]) or {"cast"}
            for n in grp: k = _db.fold(n); names.setdefault(k, n); roles.setdefault(k, set()).update(r0)
    except Exception: pass
    directing = re.search(r"\bdirect", ql) is not None
    hits, seen = [], []
    for k, n in sorted(names.items(), key=lambda kv: -len(kv[0])):
        if len(k) < 4 or " " not in k: continue
        if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", ql) and not any(k in h for h in seen):
            rs = roles.get(k, {"cast"})
            kind = "director" if "director" in rs and (directing or "cast" not in rs) else "cast"
            hits.append((kind, n)); seen.append(k)
    return hits


def director_film_count(c, name):
    """Library titles (films and scenes) crediting `name` (or any linked alias) as director."""
    import db as _db
    return c.execute("SELECT COUNT(*) FROM films WHERE person_in_any(director, ?)", (_db.ALIAS_SEP.join(_db.expand_person(c, name)),)).fetchone()[0]


def stated_count(question):
    """The number the user states for a set of titles ('Of the 209 films ...'), else None."""
    m = re.search(r"\b(\d{1,6})\s+(?:\w+\s+){0,2}?(?:films?|movies?|titles?|scenes?|pictures?)\b", question, re.I)
    return int(m.group(1)) if m and not (1880 <= int(m.group(1)) <= 2100) else None


def person_birthday(c, name):
    """(canonical name, birthday or None) over the person's linked aliases."""
    try:
        import db as _db; names = _db.expand_person(c, name)
    except Exception: names = [name]
    for n in names:
        row = c.execute("SELECT name,birthday FROM people WHERE merged_into IS NULL AND lower(name)=lower(?) AND birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", (n,)).fetchone()
        if row: return row[0], row[1]
    return name, None


def lookup_birthday(c, name):
    """One-person version of tools/fill_birthdates.py: exact name/alias match on ThePornDB; never overwrites or touches hand-edited."""
    import db, tpdb
    tok = db.get_setting(c, "tpdb_key", "") or ""
    if not tok: return None, "no ThePornDB key set"
    k = db.fold(" ".join(name.split())); nm = lambda x: db.fold(" ".join((x or "").split()))
    row = c.execute("SELECT id,birthday,edited_fields,tpdb_performer_id FROM people WHERE merged_into IS NULL AND name_norm=?", (k,)).fetchone()
    if row and "birthday" in json.loads(row[2] or "[]"): return None, "birthday was hand-edited"
    try:
        if row and row[3] not in (None, "", "-"): p = tpdb.call(f"/performers/{row[3]}", tok).get("data")
        else:
            hits = tpdb.call("/performers", tok, q=name).get("data") or []
            ex = [h for h in hits if nm(h.get("name")) == k] or [h for h in hits if any(nm(a) == k for a in (h.get("aliases") or []))]
            if len(ex) != 1: return None, ("several ThePornDB performers match" if ex else "not found on ThePornDB")
            p = ex[0]
            if not (p.get("extras") or {}).get("birthday"): p = tpdb.call(f"/performers/{p['id']}", tok).get("data") or p
    except Exception as e: return None, "ThePornDB lookup failed (" + type(e).__name__ + ")"
    bd = ((p or {}).get("extras") or {}).get("birthday")
    if not bd: return None, "ThePornDB has no birth date for them either"
    bd = bd[:10]; datetime.date.fromisoformat(bd)
    if row: c.execute("UPDATE people SET birthday=? WHERE id=? AND (birthday IS NULL OR birthday='')", (bd, row[0]))
    else: c.execute("INSERT INTO people(name,name_norm,birthday) VALUES(?,?,?)", (name, k, bd))
    c.commit(); return bd, "found on ThePornDB"


COUNT_Q = re.compile(r"(?i)\b(how many|number of|count (?:of|the|all)?)\b")
PER_Q = re.compile(r"(?i)\b(per|each|every|by (?:year|decade|month|director|studio|country|tag|genre|person|performer|actor|actress))\b|\bbreak ?down\b")


def count_question(q):
    """'How many films ...' wanting ONE number (not 'per year' / 'by director' / 'each')."""
    return bool(COUNT_Q.search(q or "")) and not PER_Q.search(q or "")


def _isnum(v): return isinstance(v, (int, float)) and not isinstance(v, bool)


def _degenerate(cols, rows):
    """Several rows whose only numbers are all 1 (one row per film/entity with COUNT(*)=1): a count gone wrong."""
    vk = [k for k, cn in enumerate(cols) if cn not in ("film_ids", "id") and cn.lower() not in ROWNUM_COLS]
    num = [k for k in vk if all(_isnum(r[k]) for r in rows)]
    return len(rows) > 1 and bool(num) and all(r[k] == 1 for r in rows for k in num)


def mentioned_tags(c, question):
    """Tags named in the question (as asked, stored names incl. synonym group members), longest first, no overlaps."""
    import db as _db
    ql = " " + re.sub(r"\s+", " ", _db.fold(question)) + " "
    groups = synonyms(c); out, seen = [], []
    names = sorted({n for (n,) in c.execute("SELECT name FROM tags WHERE COALESCE(category,'')<>'People'")}, key=lambda n: (-len(n), n))
    for n in names:
        k = _db.fold(n)
        if len(k) < 3 or any(k in h for h in seen) or re.fullmatch(r"\d+s?|\d+(st|nd|rd|th)?", k): continue  # '1979', '1970s' are years, not tags
        m = re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", ql)
        if m:
            st = [n] + [x for g in groups if any(_db.fold(y) == k for y in g) for x in g if _db.fold(x) != k]
            out.append((m.start(), n, list(dict.fromkeys(st)))); seen.append(k)
    return [(n, st) for _, n, st in sorted(out)]  # in the order the question names them


def _count_ids_sql(sql):
    """SELECT COUNT(DISTINCT x.id) AS n FROM ...  ->  SELECT DISTINCT x.id FROM ... (the titles behind the number)."""
    m = re.match(r"(?is)^\s*SELECT\s+COUNT\s*\(\s*DISTINCT\s+([\w.]+)\s*\)\s*(?:AS\s+[\"'`]?\w+[\"'`]?)?\s+(FROM\b.*)$", sql)
    return f"SELECT DISTINCT {m.group(1)} {m.group(2)}" if m else None


def direct_ids(c, r, question, hint, use_tags=True):
    """Independent check for 'how many films did <person> ... tagged <tags>': the matching title ids straight from the
    tables (same age rule: r's film_cast only holds safe credits), or None when the question isn't that shape."""
    import db as _db
    persons = [x[-1] for x in hint if len(x) == 2 and x[0] != "tags"]
    tags = [n for x in hint if len(x) == 2 and x[0] == "tags" for _, st in x[1] for n in st] if use_tags else []
    if not (persons or tags) or len(persons) > 1: return None
    q, a = "SELECT DISTINCT f.id FROM films f", []
    if persons:
        nm = [x.lower() for x in _db.expand_person(c, persons[0])]
        q += f" JOIN film_cast fc ON fc.film_id=f.id AND lower(fc.person) IN ({','.join('?' * len(nm))})"; a += nm
    if tags:
        q += (" WHERE f.id IN (SELECT ft.film_id FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved'"
              f" AND lower(t.name) IN ({','.join('?' * len(tags))}))"); a += [t.lower() for t in tags]
    return sorted(x for (x,) in r.execute(q, a))


def _other_filters(c, question, hint):
    """True when the question narrows further than person + tags (years, decades, genres, countries, studios, directors...)."""
    import db as _db
    if re.search(r"\b(1[89]|20)\d\d|\b\d0s\b|\b(before|after|since|between|rated|watched|unwatched|longer|shorter|minutes?)\b", question, re.I): return True
    ql = " " + _db.fold(question) + " "
    for col in ("primary_genre", "country", "studio"):
        for (v,) in c.execute(f"SELECT DISTINCT {col} FROM films WHERE COALESCE({col},'')<>''"):
            k = _db.fold(v)
            if len(k) >= 4 and re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", ql): return True
    return any(len(x) == 3 for x in hint)


def _msg(question, text, **kw):
    return dict(mode="sql", question=question, reading=kw.pop("reading", ""), sql="", columns=[], rows=[], answer=text,
                notes=kw.pop("notes", []), retried=False, **kw)


def ask(c, question, chat):
    import db as _db
    people = mentioned_people(c, question); hint, pre = [], []
    for kind, name in people:
        if kind == "director":
            hint.append(("director", name, [x for x in _db.expand_person(c, name)]))
            n, lib = stated_count(question), director_film_count(c, name)
            if n is not None and n != lib and len([p for p in people if p[0] == "director"]) == 1:
                pre.append(f"You said {n} films; your library has {lib} title{'' if lib == 1 else 's'} directed by {name}"
                           f"{' (counting their aliases)' if len(hint[-1][2]) > 1 else ''}, so the answer is based on those {lib}.")
            continue
        if kind != "cast": continue
        canon, bd = person_birthday(c, name)
        if bd: hint.append((name, canon)); continue
        note = []
        if not bd:
            try: got, why = lookup_birthday(c, name)
            except Exception as e: got, why = None, "lookup failed (" + type(e).__name__ + ")"
            if got: hint.append((name, name)); continue  # now known; _ro_conn rebuilds with it
            note = [f"Tried looking up {name}'s birth date: {why}."]
        return _msg(question, f"{name} has no known birth date, so per the library's age rule I can't list their titles.",
                    notes=note, birthday_status="unknown")
    tg = mentioned_tags(c, question)
    if tg: hint.append(("tags", tg))
    res = _ask_sql(c, question, chat, allow_fallback=not people, hint=hint)
    if pre: res.setdefault("notes", [])[:0] = pre
    if people and res.get("mode") == "sql" and not res["rows"] and not res.get("answer_from_error"):
        cast = [n for k, n in people if k == "cast"]
        if cast: res["notes"].insert(0, f"Only titles released when {', '.join(cast)} was 18 or older are counted.")
    return res


ROWNUM_COLS = {"#", "rank", "rn", "row", "row_number", "rownum", "no", "n#", "pos", "position"}
PCT_COLS = re.compile(r"(?i)percent|pct|share|%")


def _null_problem(cols, rows):
    """Why a result looks broken (a value column entirely NULL, or one aggregate row of only zeros/NULLs), else ''."""
    vals = [k for k, cn in enumerate(cols) if cn not in ("film_ids", "id") and cn.lower() not in ROWNUM_COLS]
    dead = [cols[k] for k in vals if all(r[k] is None for r in rows)]
    if dead:
        return (f"the query ran but column(s) {', '.join(dead)} came back empty (NULL) in every row - check the names"
                " (use the stored spelling), compute percentages as ROUND(100.0*part/NULLIF(total,0),1) with the part's"
                " condition inside SUM(CASE ...) rather than WHERE, and return part, total and percent columns")
    if len(rows) == 1 and vals and all(rows[0][k] in (None, 0, 0.0) for k in vals if not isinstance(rows[0][k], str)) \
            and any(not isinstance(rows[0][k], str) for k in vals):
        return "the query returned a single row of zeros - the filters matched nothing; check names (stored spelling) and filters"
    return ""


def _fmt(v):
    if isinstance(v, float): return (f"{v:.1f}".rstrip("0").rstrip(".")) if abs(v) < 1e15 else str(v)
    return str(v)


SUMMARY = {"rejected": ""}  # last AI sentence replaced by the plain values (returned as answer_rejected for 'Show query')


def _summary(question, cols, rows, chat, reading="", names=()):
    """One plain-English sentence from the real returned values. Row numbers/rank columns are never treated as values;
    a single aggregate row is stated as such (never 'the top result')."""
    if not rows: return "Nothing matched."
    keep = [k for k, cn in enumerate(cols) if cn not in ("film_ids",) and cn.lower() not in ROWNUM_COLS]
    if not keep: return f"{len(rows)} row(s)."
    sample = [{cols[k]: x[k] for k in keep} for x in rows[:25]]
    fallback = ""
    if len(rows) == 1:
        d = sample[0]
        others = [(k, v) for k, v in d.items() if not PCT_COLS.search(k) and isinstance(v, (int, float)) and not isinstance(v, bool)]
        pk = next((k for k in d if PCT_COLS.search(k) and isinstance(d[k], (int, float))), None)
        fallback = (f"{_fmt(d[pk])}% ({_fmt(min(others, key=lambda kv: kv[1])[1])} of {_fmt(max(others, key=lambda kv: kv[1])[1])})."
                    if pk and len(others) == 2 else "")
        if fallback and reading: fallback = fallback[:-1] + " \u2014 " + reading.rstrip(".") + "."
        fallback = fallback or "; ".join(f"{k}: {_fmt(v)}{'%' if PCT_COLS.search(k) and isinstance(v, (int, float)) else ''}" for k, v in d.items()) + "."
        prompt = ("State the answer to the question in ONE short plain-English sentence using ONLY these values from a single"
                  " aggregate result row (no SQL talk, no 'top result', no row numbers). Use the exact numbers; percentages"
                  " with a % sign. Example style: '21% of Jesús Franco's 209 directed films (44) were released 1970–1990.'"
                  " Numbers in the question (such as a stated total) may be wrong: never repeat them unless they equal one of"
                  " these values; the values are the truth for this library." +
                  (" Spell names as: " + ", ".join(names) + "." if names else "") + "\n"
                  "Question: " + question + "\nValues: " + " | ".join(f"{k}={_fmt(v)}" for k, v in d.items()))
    else:
        prompt = ("Answer the question in ONE short plain-English sentence using only these query results (no SQL talk)."
                  " These rows ARE the answer to the question (already filtered for it). They are ranked as shown: name the top"
                  " result with its number; if several rows tie for the top value, name all of them. For a breakdown (per decade"
                  " etc.) summarise the pattern with the biggest values. There is no row-number column; never cite a position"
                  " as a value.\nQuestion: " + question + "\nResults (one per line):\n" +
                  "\n".join(" | ".join(f"{k}={_fmt(v)}" for k, v in d.items()) for d in sample)[:6000])
    try: ans = re.sub(r"<think>.*?</think>", "", chat(prompt) or "", flags=re.S).strip().strip('"')
    except Exception: ans = ""
    if len(rows) == 1:
        d = sample[0]
        nums = [float(v) for v in d.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        pct = [float(v) for k, v in d.items() if PCT_COLS.search(k) and isinstance(v, (int, float))]
        st = stated_count(question)
        qn = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", question.replace(",", ""))} - ({float(st)} if st is not None else set())
        near = lambda f, vs: any(abs(v - f) < 0.051 or abs(round(v) - f) < 0.001 for v in vs)
        found = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", ans.replace(",", ""))]
        ok = bool(ans) and "top result" not in ans.lower() and all(near(f, nums) or f in qn for f in found) \
            and (all(any(near(f, [v]) for f in found) for v in pct) if pct else any(near(f, nums) for f in found))
        if not ok: SUMMARY["rejected"] = ans; ans = fallback
    elif ans:  # several rows: every number in the sentence must be one of the shown values (or from the question)
        vals = [float(v) for d in sample for v in d.values() if _isnum(v)]
        vals += [float(x) for d in sample for v in d.values() if isinstance(v, str) for x in re.findall(r"\d+(?:\.\d+)?", v)]
        qn = {float(x) for x in re.findall(r"\d+(?:\.\d+)?", question.replace(",", ""))}
        found = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", ans.replace(",", ""))]
        if any(not any(abs(f - v) < 0.051 for v in vals) and f not in qn for f in found) or re.search(r"(?i)\b\d{4} decade\b", ans):
            SUMMARY["rejected"] = ans; d0 = sample[0]
            lab = next((v for v in d0.values() if isinstance(v, str)), None) or next((_fmt(v) for k, v in d0.items() if re.search(r"(?i)year|decade|month", k)), None)
            num = [(k, v) for k, v in d0.items() if _isnum(v) and not re.search(r"(?i)^(year|decade|month)$", k)]
            ans = (f"{lab}: {', '.join(f'{_fmt(v)} {k}' for k, v in num[:2])} (first of {len(rows)} rows shown below)." if lab and num
                   else f"{len(rows)} rows (shown below).")
    return ans or fallback or f"{len(rows)} row(s)."


def _ask_sql(c, question, chat, allow_fallback=True, hint=()):
    """chat(prompt)->text. Returns a result dict; mode 'sql' or (fallback) the old spec report."""
    SUMMARY.pop("direct", None); SUMMARY.pop("fallback", None)
    tries, err, reading, sql = [], None, "", ""
    try:
        r, everyone, safe = _ro_conn(c)
    except Exception as e:
        r, err = None, e
    prompt = sql_prompt(c, question, hint) if r else None
    for attempt in range(2):
        if not r: break
        try:
            reading, sql = _parse_sql_reply(chat(prompt)); sql = fix_sql(sql)
            cols, rows, more, dropped = run_sql(r, sql, everyone, safe)
            if attempt == 0 and not rows: raise ValueError("the query ran but returned no rows; check names, joins and filters")
            if attempt == 0:
                bad = _null_problem(cols, rows)
                if bad: raise ValueError(bad)
                if count_question(question) and _degenerate(cols, rows):
                    raise ValueError(f"the question asks for ONE number but the query returned {len(rows)} rows that each count 1"
                                     " (one row per film). Return a single row: SELECT COUNT(DISTINCT f.id) AS films ... with no GROUP BY")
            break
        except Exception as e:
            err = e; tries.append(dict(sql=sql, error=str(e)))
            prompt = (sql_prompt(c, question, hint) + f"\n\nYour previous query was:\n{sql}\nIt failed with: {e}\nFix it. Reply with JSON only as before.")
    else:
        r = None
    if r is None and not allow_fallback:
        why = (tries[-1]["error"] if tries else str(err)) if (tries or err) else "unknown error"
        return _msg(question, "I couldn't answer that: " + why + ".", notes=["This question is about a person, so I didn't substitute a ready-made report."], answer_from_error=True)
    if r is None:
        try:
            res = run(c, parse_spec(chat(ask_prompt(c, question))))
            res.update(question=question, mode="spec", sql_errors=tries)
            res.setdefault("notes", []).insert(0, "Couldn't write a query for that, so this is the closest ready-made report.")
            return res
        except Exception as e2:
            raise ValueError(f"Could not answer: {err or e2}")
    notes, count_ids = [], None
    cq = count_question(question)
    if cq and len(rows) == 1 and not re.search(r"(?i)percent|%|share|proportion|fraction|ratio", question) \
            and any(PCT_COLS.search(cn) for cn in cols):  # 'Of all X, how many are Y?' answered as a percentage: the count is the part
        nk = [k for k, cn in enumerate(cols) if not PCT_COLS.search(cn) and cn not in ("film_ids", "id") and cn.lower() not in ROWNUM_COLS and _isnum(rows[0][k])]
        if len(nk) >= 2 and all(rows[0][k] is not None for k in nk):
            part = min(rows[0][k] for k in nk); li = cols.index("film_ids") if "film_ids" in cols else None
            notes.append(f"The query worked out a share; the question asks how many, so the count ({_fmt(part)}) is shown.")
            cols, rows = (["films", "film_ids"], [[part, rows[0][li]]]) if li is not None else (["films"], [[part]])
    if cq and _degenerate(cols, rows):  # still one row per film after the retry: collapse to the total
        li = cols.index("id") if "id" in cols else None
        try: n = r.execute("SELECT COUNT(*) FROM (" + re.sub(r"(?is)\s+LIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", "", check_sql(sql)) + ")").fetchone()[0]
        except Exception: n = len(rows)
        if li is not None: count_ids = sorted({x[li] for x in rows if isinstance(x[li], int)})
        cols, rows = ["films"], [[n]]; more = False
        notes.append(f"The query listed one row per title, so they were added up: {n}.")
    if cq and len(rows) == 1:
        vk = [k for k, cn in enumerate(cols) if cn not in ("film_ids", "id") and cn.lower() not in ROWNUM_COLS and _isnum(rows[0][k])]
        if len(vk) == 1:
            n = rows[0][vk[0]]
            if count_ids is None and "film_ids" in cols and rows[0][cols.index("film_ids")]:
                count_ids = sorted({int(x) for x in str(rows[0][cols.index("film_ids")]).split(",") if x.strip().isdigit()})
            if count_ids is None and _count_ids_sql(sql):
                try: count_ids = sorted({x for (x,) in r.execute(_count_ids_sql(sql)) if isinstance(x, int)})
                except Exception: count_ids = None
            if count_ids is not None and len(count_ids) != n: count_ids = None
            try: d = direct_ids(c, r, question, hint)
            except Exception: d = None
            tags = [a for x in hint if len(x) == 2 and x[0] == "tags" for a, _ in x[1]]
            if d is not None and not tags and re.search(r"(?i)\btag", question): d = None  # a tag we couldn't recognise: no person-only check
            if d is not None and tags and not re.search(r"(?i)\btag", question): d = None  # words that happen to be tag names: no check
            who = next((x[-1] for x in hint if len(x) == 2 and x[0] != "tags"), None)
            syn = list(dict.fromkeys(n_ for x in hint if len(x) == 2 and x[0] == "tags" for _, st in x[1] for n_ in st if n_ not in tags))
            sent = lambda k: ((f"{who} is credited in {k} title{'' if k == 1 else 's'}" if who else f"{k} title{'' if k == 1 else 's'} are")
                              + (f" tagged {' or '.join(tags)}" if tags else "") + (f" (counting the synonyms {', '.join(syn)})" if syn else "") + ".")
            if d is not None:
                if len(d) == n:
                    notes.append(f"Checked against a direct count of the library: {n}.")
                    if count_ids is None: count_ids = d
                    SUMMARY["fallback"] = sent(n)
                elif not _other_filters(c, question, hint):
                    notes.append(f"The AI's query counted {_fmt(n)}; a direct count of the library found {len(d)}, which is the answer shown.")
                    cols, rows, count_ids = [cols[vk[0]]], [[len(d)]], d
                    SUMMARY["direct"] = sent(len(d))
    if len(rows) == 1 and any(PCT_COLS.search(cn) for cn in cols) and not _other_filters(c, question, hint):
        pk = next(k for k, cn in enumerate(cols) if PCT_COLS.search(cn))
        nk = [k for k, cn in enumerate(cols) if k != pk and cn not in ("film_ids", "id") and cn.lower() not in ROWNUM_COLS and _isnum(rows[0][k])]
        has_tags = any(len(x) == 2 and x[0] == "tags" for x in hint)
        try:
            tot = direct_ids(c, r, question, hint, use_tags=False) if has_tags else None
            part = direct_ids(c, r, question, hint) if tot is not None else None
        except Exception: tot = part = None
        if tot is not None and part is not None and tot and any(len(x) == 2 and x[0] != "tags" for x in hint):
            a, b = sorted((rows[0][k] for k in nk[:2]), key=lambda v: v or 0) if len(nk) >= 2 else (None, None)
            pct = round(100.0 * len(part) / len(tot), 1)
            if (a, b) == (len(part), len(tot)) and abs((rows[0][pk] or 0) - pct) < 0.11:
                notes.append(f"Checked against a direct count of the library: {len(part)} of {len(tot)}.")
            else:
                notes.append(f"The AI's query gave {_fmt(a)} of {_fmt(b)} ({_fmt(rows[0][pk])}%); a direct count of the library found"
                             f" {len(part)} of {len(tot)}, which is the answer shown.")
                cols, rows = ["matching", "total", "percent", "film_ids"], [[len(part), len(tot), pct, ",".join(map(str, part))]]
                who = next(x[-1] for x in hint if len(x) == 2 and x[0] != "tags")
                tg = [a_ for x in hint if len(x) == 2 and x[0] == "tags" for a_, _ in x[1]]
                SUMMARY["direct"] = f"{_fmt(pct)}% of {who}'s {len(tot)} titles ({len(part)}) are tagged {' or '.join(tg)}."
    if dropped: notes.append(f"{dropped} row(s) were hidden because they involve performers without a known adult birth date.")
    if more: notes.append(f"Showing the first {SQL_MAX_ROWS} rows.")
    if tries and _null_problem(cols, rows): notes.append("Some values came back empty even after a retry, so treat this answer with care.")
    SUMMARY["rejected"] = ""
    direct, fb = SUMMARY.pop("direct", None), SUMMARY.pop("fallback", None)
    answer = direct or _summary(question, cols, rows, chat, reading, [x[1] if len(x) == 3 else x[-1] for x in hint if x[0] != "tags"])
    if fb and not direct and SUMMARY["rejected"]: answer = fb  # the AI's sentence had wrong numbers: say the checked count plainly
    r.close()
    return dict(mode="sql", question=question, reading=reading, sql=sql, columns=cols, rows=rows, answer=answer,
                notes=notes, retried=bool(tries), answer_rejected=SUMMARY["rejected"], chart_hint=PARSED["chart"],
                directors=[n for x in hint if len(x) == 3 for n in x[2]], mentions_cast=any(len(x) != 3 and x[0] != "tags" for x in hint),
                count_ids=count_ids)


# ---------- Dashboard (home page summary; read-only) ----------
NON_ADULT_SQL = "COALESCE(kind,'')<>'scene' AND COALESCE(primary_genre,'') NOT IN ('Hardcore','Softcore')"
NO_POSTER_SQL = ("COALESCE(custom_poster,'')='' AND COALESCE(poster,'')='' "
                 "AND NOT (kind='scene' AND COALESCE(poster_wide,'')<>'')")


def _mark_in_library(c, plist, pc):
    """Set in_library on each person dict: credited (name, multi-word alias or merged person) on a library title, or a
    library title in their cached TMDB credits. A person row stays after their only title is removed from the library, so
    the 'People without a birth date' list says so instead of implying they are still in it."""
    import db
    names, tm = set(), set()
    for r in c.execute("SELECT tmdb_id, " + ", ".join(db.PEOPLE_ROLES.values()) + " FROM films"):
        if r[0]: tm.add(r[0])
        for v in r[1:]:
            for n in (v or "").split(","):
                n = " ".join(n.split())
                if n: names.add(db.fold(n))
    cols = ", ".join(x for x in ("aliases", "credits_json") if x in pc) or "NULL"
    for p in plist:
        row = c.execute(f"SELECT {cols} FROM people WHERE id=?", (p["id"],)).fetchone()
        keys = {db.fold(n) for n in db.expand_person(c, p["name"])}
        try: keys |= {db.fold(" ".join(a.split())) for a in json.loads((row[0] if "aliases" in pc else None) or "[]") if db._alias_ok(a)}
        except Exception: pass
        hit = bool(keys & names)
        if not hit and "credits_json" in pc:
            try: cr = json.loads(row[-1] or "{}")
            except Exception: cr = {}
            hit = any(x.get("id") in tm for x in (cr.get("cast") or []) + (cr.get("crew") or []) if isinstance(x, dict))
        p["in_library"] = hit


def dashboard(c, today=None, S="1"):
    """Library summary for the Dashboard. Text and numbers only (never poster paths). 'Recently added' and 'On this day'
    leave out scenes and Hardcore/Softcore films; 'Classic Porn' = films (not scenes) whose primary genre is Hardcore.
    `S` is an SQL condition on films.id limiting everything except the top scope tiles to the selected scope
    (e.g. "id IN (SELECT id FROM temp.dash_scope)"); "1" = the whole library."""
    today = today or datetime.date.today()
    one = lambda sql, a=(): c.execute(sql, a).fetchone()[0] or 0
    FILM = "COALESCE(kind,'')<>'scene'"
    SF = "1" if S == "1" else S.replace("id IN", "film_id IN", 1)
    pc = {r[1] for r in c.execute("PRAGMA table_info(people)")}  # people table/columns appear once People is first used
    PW = "merged_into IS NULL" if "merged_into" in pc else "1"
    s = dict(films=one(f"SELECT COUNT(*) FROM films WHERE {FILM}"),
             scenes=one("SELECT COUNT(*) FROM films WHERE kind='scene'"),
             classic=one(f"SELECT COUNT(*) FROM films WHERE {FILM} AND primary_genre=?", (ADULT_GENRE,)),
             people=one(f"SELECT COUNT(*) FROM people WHERE {PW}") if pc else 0,
             tags=one(f"SELECT COUNT(DISTINCT tag_id) FROM film_tags WHERE status='approved' AND {SF}") if S != "1" else one("SELECT COUNT(*) FROM tags"),
             tag_uses=one(f"SELECT COUNT(*) FROM film_tags WHERE status='approved' AND {SF}"),
             runtime_min=one(f"SELECT SUM(runtime) FROM films WHERE runtime>0 AND {S}"),
             bytes=one(f"SELECT SUM(size) FROM files WHERE COALESCE(missing,0)=0 AND {SF}"),
             files=one(f"SELECT COUNT(*) FROM files WHERE {SF}"),
             in_scope=one(f"SELECT COUNT(*) FROM films WHERE {S}"))
    if S != "1":  # people credited on titles in scope (distinct names over every role column)
        cols = [x for x in ("director", "cast_", "writers", "producers", "cinematographer", "composer")
                if x in {r[1] for r in c.execute("PRAGMA table_info(films)")}]
        names = set()
        for row in c.execute(f"SELECT {','.join(cols)} FROM films WHERE {S}"):
            for v in row:
                for x in _split(v): names.add(" ".join(x.split()).casefold())
        s["people_in_scope"] = len(names)
    dec = [dict(decade=d, films=f or 0, scenes=sc or 0) for d, f, sc in c.execute(
        f"SELECT (year/10)*10 d, SUM({FILM}), SUM(kind='scene') FROM films WHERE year>0 AND {S} GROUP BY d ORDER BY d")]
    undated = one(f"SELECT COUNT(*) FROM films WHERE (year IS NULL OR year<=0) AND {S}")
    genres = [dict(name=n, n=k) for n, k in c.execute(
        f"SELECT primary_genre, COUNT(*) FROM films WHERE {FILM} AND {S} AND COALESCE(primary_genre,'')<>'' GROUP BY primary_genre COLLATE NOCASE ORDER BY 2 DESC, 1 LIMIT 8")]
    no_genre = one(f"SELECT COUNT(*) FROM films WHERE {FILM} AND {S} AND COALESCE(primary_genre,'')=''")
    cc, dc = {}, {}
    for co, d in c.execute(f"SELECT country, director FROM films WHERE {FILM} AND {S}"):
        for x in _split(co): cc[x] = cc.get(x, 0) + 1
        for x in _split(d): dc[x] = dc.get(x, 0) + 1
    top = lambda m, n: [dict(name=k, n=v) for k, v in sorted(m.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:n]]
    studios = []
    if S != "1":  # scene studios in scope (shown instead of directors when the scope is mostly scenes)
        studios = [dict(name=n, n=k) for n, k in c.execute(
            f"SELECT studio, COUNT(*) FROM films WHERE kind='scene' AND {S} AND COALESCE(studio,'')<>'' GROUP BY studio COLLATE NOCASE ORDER BY 2 DESC, 1 LIMIT 10")]
    recent = [dict(id=i, title=t, year=y, added_at=a) for i, t, y, a in c.execute(
        f"SELECT id, title, year, added_at FROM films WHERE {NON_ADULT_SQL} AND {S} AND added_at IS NOT NULL ORDER BY added_at DESC, id DESC LIMIT 10")]
    md = today.strftime("-%m-%d")
    otd = [dict(id=i, title=t, year=int(rd[:4]), release_date=rd) for i, t, rd in c.execute(
        f"SELECT id, title, release_date FROM films WHERE {NON_ADULT_SQL} AND {S} AND release_date LIKE ? ORDER BY release_date, title COLLATE NOCASE",
        ("____" + md,)) if rd[:4].isdigit()]
    otd_all = one(f"SELECT COUNT(*) FROM films WHERE {S} AND release_date LIKE ?", ("____" + md,))
    NB = f"FROM people WHERE {PW} AND COALESCE(birthday,'')=''"
    no_bday = [dict(id=i, name=n, source=src) for i, n, src in c.execute(
        f"SELECT id, name, {'source' if 'source' in pc else 'NULL'} {NB} ORDER BY name COLLATE NOCASE LIMIT 500")] if pc else []
    if no_bday: _mark_in_library(c, no_bday, pc)
    health = dict(  # always the whole library (the fix-it lists use their own filters)
        no_poster=one(f"SELECT COUNT(*) FROM films WHERE {NO_POSTER_SQL}"),
        no_poster_scenes=one(f"SELECT COUNT(*) FROM films WHERE kind='scene' AND {NO_POSTER_SQL}"),
        needs_tmdb=one(f"SELECT COUNT(*) FROM films WHERE tmdb_status='needs_match'"),
        missing=one(f"SELECT COUNT(*) FROM films WHERE missing=1"),
        no_birthday=one(f"SELECT COUNT(*) {NB}") if pc else 0,
        untagged=one(f"SELECT COUNT(*) FROM films WHERE id NOT IN (SELECT film_id FROM film_tags WHERE status='approved')"),
        pending=one(f"SELECT COUNT(DISTINCT film_id) FROM film_tags WHERE status='pending'"))
    return dict(stats=s, decades=dec, undated=undated, genres=genres, no_genre=no_genre, countries=top(cc, 8),
                n_countries=len(cc), directors=top(dc, 10), studios=studios, recent=recent, on_this_day=otd, on_this_day_all=otd_all,
                on_this_day_hidden=max(0, otd_all - len(otd)), today=today.isoformat(), health=health,
                no_birthday_people=no_bday, reports=REPORTS)


# ---------- Dashboard explore (drill-down charts; read-only) ----------
# Month of a title for the explore view: from release_date when it is at least YYYY-MM and its year matches films.year,
# else 0 ('unknown month'). app.build_query's `month` filter uses this same expression, so counts always match the list.
MONTH_SQL = ("(CASE WHEN length(COALESCE(f.release_date,''))>=7 AND substr(f.release_date,1,4)=printf('%04d',f.year) "
             "AND substr(f.release_date,6,2) GLOB '[0-9][0-9]' THEN CAST(substr(f.release_date,6,2) AS INTEGER) ELSE 0 END)")
ADULT_GENRES = ("hardcore", "softcore")  # explore cast counts use the Reports age rule (_adult_ok above)


def explore(c, where, args, lim=12):
    """Aggregates over the titles matched by app.build_query(where, args) (the same filter as the library list, so
    `total` always equals the list count). Text and numbers only."""
    import db
    fc = {r[1] for r in c.execute("PRAGMA table_info(films)")}
    roles = [(k, col) for k, col in db.PEOPLE_ROLES.items() if col in fc]
    cols = ["f.id", "f.year", "f.release_date", "COALESCE(f.kind,'')='scene'", "f.primary_genre", "tag_norm(f.primary_genre)",
            "f.country", "f.studio", MONTH_SQL] + [f"f.{col}" for _, col in roles]
    rows = c.execute(f"SELECT {','.join(cols)} FROM films f {where}", args).fetchall()
    nrm = lambda x: " ".join(x.split()).casefold()
    dec, yrs, mon = {}, {}, {}
    gen, gname, cty, dirs, stu, cast = {}, {}, {}, {}, {}, {}
    rolen = {k: set() for k, _ in roles}
    films = scenes = undated = no_genre = 0
    pc = {r[1] for r in c.execute("PRAGMA table_info(people)")}
    bdays = {}
    if pc:
        for n, b in c.execute("SELECT name,birthday FROM people WHERE " + ("merged_into IS NULL AND " if "merged_into" in pc else "") +
                              "birthday GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"):
            try: bdays[nrm(n)] = datetime.date.fromisoformat(b)
            except ValueError: pass
    adult_titles = 0
    for r in rows:
        fid, y, rd, sc, pg, pgk, co, st, m = r[:9]
        credits = dict(zip([k for k, _ in roles], r[9:]))
        k2 = 1 if sc else 0
        if sc: scenes += 1
        else: films += 1
        if y and y > 0:
            for mp, key in ((dec, (y // 10) * 10), (yrs, y)):
                v = mp.setdefault(key, [0, 0]); v[k2] += 1
        else: undated += 1
        v = mon.setdefault(m or 0, [0, 0]); v[k2] += 1
        if not sc:
            if pg and pg.strip():
                gen[pgk] = gen.get(pgk, 0) + 1; gname.setdefault(pgk, {}); gname[pgk][pg] = gname[pgk].get(pg, 0) + 1
            else: no_genre += 1
            for x in _split(co): cty.setdefault(nrm(x), [x, 0])[1] += 1
            for x in _split(credits.get("director")): dirs.setdefault(nrm(x), [x, 0])[1] += 1
        if st and st.strip(): stu.setdefault(db.studio_key(st), [st.strip(), 0])[1] += 1
        adult = sc or (pg or "").strip().lower() in ADULT_GENRES
        if adult: adult_titles += 1
        for x in _split(credits.get("cast")):
            k = nrm(x)
            if adult:  # adult titles: only performers with a known birth date who were adults at release
                bd = bdays.get(k)
                if not bd or not _adult_ok(bd, rd, y): continue
            cast.setdefault(k, [x, 0])[1] += 1
        for rk, _ in roles:
            for x in _split(credits.get(rk)):
                if rk == "cast" and adult and not (bdays.get(nrm(x)) and _adult_ok(bdays[nrm(x)], rd, y)): continue
                rolen[rk].add(nrm(x))
    rank = lambda m, n: [dict(name=v[0], n=v[1]) for v in sorted(m.values(), key=lambda v: (-v[1], v[0].lower()))[:n]]
    genres = sorted(([max(gname[k].items(), key=lambda kv: kv[1])[0], n] for k, n in gen.items()), key=lambda v: (-v[1], v[0].lower()))
    ids_sql = f"SELECT f.id FROM films f {where}"
    tags = [dict(name=n, n=k) for n, k in c.execute(
        f"SELECT MIN(t.name), COUNT(DISTINCT ft.film_id) FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.status='approved' "
        f"AND ft.film_id IN ({ids_sql}) GROUP BY tag_norm(t.name) ORDER BY 2 DESC, 1 LIMIT 20", args)]
    ser = lambda m: [dict(k=k, films=v[0], scenes=v[1]) for k, v in sorted(m.items())]
    years = [k for k in yrs]
    base = [dict(k=d, films=f or 0, scenes=s or 0) for d, f, s in c.execute(
        "SELECT (year/10)*10 d, SUM(COALESCE(kind,'')<>'scene'), SUM(kind='scene') FROM films WHERE year>0 GROUP BY d ORDER BY d")]
    base_y = [dict(k=d, films=f or 0, scenes=s or 0) for d, f, s in c.execute(
        "SELECT year, SUM(COALESCE(kind,'')<>'scene'), SUM(kind='scene') FROM films WHERE year>0 GROUP BY year ORDER BY year")]
    return dict(total=len(rows), films=films, scenes=scenes, undated=undated, no_genre=no_genre,
                year_min=min(years) if years else None, year_max=max(years) if years else None,
                decades=ser(dec), years=ser(yrs), months=ser(mon),
                genres=[dict(name=g, n=n) for g, n in genres[:lim]], n_genres=len(genres),
                countries=rank(cty, lim), n_countries=len(cty), directors=rank(dirs, lim), n_directors=len(dirs),
                studios=rank(stu, 15), n_studios=len(stu), tags=tags, cast=rank(cast, lim), adult_titles=adult_titles,
                known_birthdays=len(bdays), roles={k: len(v) for k, v in rolen.items()},
                base_decades=base, base_years=base_y)
