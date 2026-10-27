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
    return r, everyone, {s.lower() for s in safe}


def sql_schema(c):
    tags = [t for t in options(c)["tags"][:60]]
    return (
        "SQLite tables (use ONLY these):\n"
        " films(id, title, year, release_date 'YYYY-MM-DD' or NULL, director (comma-separated), country, studio, primary_genre,"
        " kind ('scene' for scenes, NULL for films))\n"
        " film_directors(film_id, director)  -- one row per film per director (already split)\n"
        " film_cast(film_id, person)  -- one row per film per performer (already split); use this for anything about performers/cast\n"
        " people(name, birth_year, ...)  -- performers/people\n"
        " tags(id, name, category); film_tags(film_id, tag_id, status) -- only count status='approved'\n"
        "Notes:\n"
        " - 'Classic Porn' / porn / adult films = films WHERE " + _ADULT_ON + ".\n"
        " - 'Exploitation', 'Giallo', etc. are usually TAGS (match tags.name case-insensitively), not genres. Common tags: " + json.dumps(tags) + "\n"
        " - Tag synonym groups (treat members as the same tag, use IN (...)): " + json.dumps(synonyms(c)) + "\n"
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
        " - Never add NOT IN / NOT EXISTS sub-queries that exclude the person's own films; at most add d.director NOT LIKE '<that person>'.\n")


def sql_prompt(c, question):
    return ("You answer questions about a personal film library by writing ONE SQLite SELECT query.\n" + sql_schema(c) +
            'Reply with JSON only: {"reading": "<one short line: how you understood the question>", "sql": "<one SELECT or WITH query, no semicolon>"}\n'
            "Limit results to at most 50 rows unless asked otherwise.\nQuestion: " + question.strip()[:500])


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
    return str(v.get("reading") or "").strip(), str(v.get("sql") or "").strip()


def check_sql(sql):
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if not re.match(r"(?is)^(select|with)\b", s): raise ValueError("Only SELECT queries are allowed")
    if ";" in s: raise ValueError("Only one statement is allowed")
    m = _BAD.search(re.sub(r"'(?:[^']|'')*'", "''", s))
    if m: raise ValueError(f"'{m.group(0)}' is not allowed in the query")
    return s


def run_sql(r, sql, everyone, safe):
    s = check_sql(sql)
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


def ask(c, question, chat):
    """chat(prompt)->text. Returns a result dict; mode 'sql' or (fallback) the old spec report."""
    tries, err, reading, sql = [], None, "", ""
    try:
        r, everyone, safe = _ro_conn(c)
    except Exception as e:
        r, err = None, e
    prompt = sql_prompt(c, question) if r else None
    for attempt in range(2):
        if not r: break
        try:
            reading, sql = _parse_sql_reply(chat(prompt))
            cols, rows, more, dropped = run_sql(r, sql, everyone, safe)
            if attempt == 0 and not rows: raise ValueError("the query ran but returned no rows; check names, joins and filters")
            break
        except Exception as e:
            err = e; tries.append(dict(sql=sql, error=str(e)))
            prompt = (sql_prompt(c, question) + f"\n\nYour previous query was:\n{sql}\nIt failed with: {e}\nFix it. Reply with JSON only as before.")
    else:
        r = None
    if r is None:
        try:
            res = run(c, parse_spec(chat(ask_prompt(c, question))))
            res.update(question=question, mode="spec", sql_errors=tries)
            res.setdefault("notes", []).insert(0, "Couldn't write a query for that, so this is the closest ready-made report.")
            return res
        except Exception as e2:
            raise ValueError(f"Could not answer: {err or e2}")
    notes = []
    if dropped: notes.append(f"{dropped} row(s) were hidden because they involve performers without a known adult birth date.")
    if more: notes.append(f"Showing the first {SQL_MAX_ROWS} rows.")
    answer = ""
    try:
        sample = [dict(zip(cols, [v for v in x])) for x in rows[:25]]
        for d in sample: d.pop("film_ids", None)
        answer = re.sub(r"<think>.*?</think>", "", chat(
            "Answer the question in ONE short plain-English sentence using only these query results (no SQL talk). "
            "Rows are ranked/ordered as shown; mention the top result with its number, or for a breakdown (per decade etc.) summarise the pattern with the biggest values. "
            "If there are no rows say nothing matched.\nQuestion: " + question + "\nColumns: " + json.dumps(cols) +
            "\nRows: " + json.dumps(sample, default=str)[:6000]) or "", flags=re.S).strip().strip('"')
    except Exception: answer = ""
    r.close()
    return dict(mode="sql", question=question, reading=reading, sql=sql, columns=cols, rows=rows, answer=answer,
                notes=notes, retried=bool(tries))
