"""Scenes libraries: filename parsing and ThePornDB (api.theporndb.net) lookups. TMDB is never used for scenes.

Recommended layout:  <Library>\\<Studio>\\<Studio> - YYYY-MM-DD - <Performer 1>, <Performer 2> - <Title>.<ext>
Also understood: 'StudioName Performer Name 720p 16102022.mp4' (CamelCase studio, DDMMYYYY / YYYY.MM.DD dates)."""
import json, os, re, time, urllib.request, urllib.parse, urllib.error

API = "https://api.theporndb.net"
JUNK = re.compile(r"\b(2160p|1080p|720p|480p|4k|uhd|xxx|mp4|web-?dl|webrip|hevc|x264|x265|h264|h265)\b", re.I)

def camel_split(s):
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s).strip()

def _date(s):
    for pat, order in ((r"\b((?:19|20)\d\d)[-._ ](\d\d)[-._ ](\d\d)\b", "ymd"), (r"\b(\d\d)[-._ ](\d\d)[-._ ]((?:19|20)\d\d)\b", "dmy"),
                       (r"\b((?:19|20)\d\d)(\d\d)(\d\d)\b", "ymd"), (r"\b(\d\d)(\d\d)((?:19|20)\d\d)\b", "dmy"), (r"\b(\d\d)[-._](\d\d)[-._](\d\d)\b", "yymd")):
        m = re.search(pat, s)
        if not m: continue
        a, b, c = m.groups()
        y, mo, d = (a, b, c) if order == "ymd" else (c, b, a) if order == "dmy" else ("20" + a, b, c)
        if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31: return f"{y}-{mo}-{d}", m
    return None, None

def parse_name(path):
    """-> dict(studio, date, performers[list], title). Missing parts are None/[]."""
    segs = re.split(r"[\\/]", path or "")
    stem = os.path.splitext(segs[-1])[0]
    parent = segs[-2] if len(segs) > 1 else ""
    parts = [p.strip() for p in stem.split(" - ")]
    out = dict(studio=None, date=None, performers=[], title=None)
    if len(parts) >= 3:
        rest = []
        for p in parts:
            d, m = _date(p)
            if d and m and m.group(0) == p.strip(): out["date"] = d
            else: rest.append(p)
        out["studio"] = rest[0] if rest else None
        if len(rest) >= 2: out["performers"] = [x.strip() for x in re.split(r",|&| and ", rest[1]) if x.strip()]
        if len(rest) >= 3: out["title"] = " - ".join(rest[2:])
        return out
    s = re.sub(r"[._]+", " ", stem)
    d, m = _date(s)
    if m: out["date"] = d; s = s[:m.start()] + " " + s[m.end():]
    s = re.sub(r"\s+", " ", JUNK.sub(" ", s)).strip()
    toks = s.split(" ")
    if toks and re.search(r"[a-z][A-Z]", toks[0]):  # 'BeautyAndTheSenior Nikky Thorne'
        out["studio"] = camel_split(toks[0]); toks = toks[1:]
    elif parent and parent.lower() not in ("", "porn scenes"):
        out["studio"] = parent
    name = " ".join(toks).strip()
    if name: out["performers"] = [x.strip() for x in re.split(r",|&| and ", name) if x.strip()]
    return out

def default_title(p):
    return p.get("title") or (" & ".join(p["performers"]) if p.get("performers") else None)

def parse_id(v):
    """ThePornDB scene id from a UUID/numeric id/slug or a theporndb.net/scenes/<id-or-slug> URL."""
    s = (v or "").strip()
    if not s: return None
    m = re.search(r"theporndb\.net/(?:[a-z]{2}/)?scenes/([^/?#\s]+)", s)
    if m: return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-_]+", s): return s
    raise ValueError(f"Not a ThePornDB scene id or URL: {s}")

def call(path, token, **params):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "FilmTagger"})
    with urllib.request.urlopen(req, timeout=25) as r: return json.loads(r.read())

def get_scene(token, sid): return call(f"/scenes/{urllib.parse.quote(str(sid))}", token).get("data") or {}

def lookup(token, path):
    """Confident ThePornDB match for a file (fingerprint, parse, studio+date, studio+text, text), or None."""
    r = identify(token, path)
    return r["scene"] if r and r.get("confident") else None

def lookup_legacy(token, path):
    p = parse_name(path)
    q = " ".join(x for x in [p.get("studio") or "", " ".join(p.get("performers") or []), p.get("title") or "", p.get("date") or ""] if x).strip()
    res = (call("/scenes", token, parse=q).get("data") or []) if q else []
    if not res and p.get("studio") and p.get("date"):
        res = call("/scenes", token, site=p["studio"], date=p["date"]).get("data") or []
    if not res: return None
    if p.get("date"):
        same = [r for r in res if (r.get("date") or "")[:10] == p["date"]]
        if same: res = same
    return res[0]

def _strip_q(t):
    import db
    return db.strip_title_quotes(t)

def wide_image(s):
    """Full-size landscape cover (the poster is ThePornDB's own 2:3 crop of it), or None."""
    bg = s.get("background") if isinstance(s.get("background"), dict) else {}
    u = bg.get("large") or bg.get("full") or s.get("image")
    return u if u and u != s.get("poster") else None

def scene_fields(s):
    """Map a ThePornDB scene resource to Film Tagger columns."""
    site = s.get("site") or {}
    img = s.get("poster") or s.get("image") or ((s.get("background") or {}).get("large"))
    date = (s.get("date") or "")[:10] or None
    dur = s.get("duration")
    return dict(title=_strip_q(s.get("title")), studio=site.get("name"), release_date=date, year=int(date[:4]) if date else None,
                cast_=", ".join(p.get("name") for p in (s.get("performers") or []) if p.get("name")) or None,
                runtime=round(dur / 60) if isinstance(dur, (int, float)) and dur else None, plot=s.get("description") or None,
                poster=img or None, poster_wide=wide_image(s), tpdb_id=str(s.get("id") or s.get("_id") or "") or None,
                tags=[t.get("name") for t in (s.get("tags") or []) if t.get("name")])

# ---------- improved multi-strategy identification (v1.0.28) ----------
import struct, difflib
SITE_JUNK = re.compile(r"(watch on\b.*$|pornhoarder\S*|hqporner\S*|\b4kporn\b|\beporn\b|\bxvideos\b|\bfull hi\b|\b1080hd\b|\b\d{3,4}\s*x\s*\d{3,4}\b|\b\d{3,4}p\b|\(\d\)|\.mp4\b)", re.I)
STOP = {"and", "the", "a", "in", "of", "with", "for", "to", "on", "my", "is", "her", "his", "me", "you", "i", "it", "at", "by", "s", "t"}

def oshash(path):
    """OpenSubtitles hash (as used by ThePornDB/Stash): size + 64-bit sums of the first and last 64 KiB."""
    sz = os.path.getsize(path); h = sz; ch = 65536
    if sz < ch * 2: return None
    with open(path, "rb") as f:
        for off in (0, sz - ch):
            f.seek(off)
            for (x,) in struct.iter_unpack("<Q", f.read(ch)): h = (h + x) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h

def nrm(s): return re.sub(r"[^a-z0-9]", "", (s or "").lower())
def words(s): return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower().replace("'", "")) if w not in STOP]

def clean_stem(stem):
    """Filename stem -> searchable text: drops site/rip junk, resolutions, '(1)', dots/underscores, CamelCase studio glue."""
    s = SITE_JUNK.sub(" ", stem)
    s = re.sub(r"[._]+", " ", s)
    s = re.sub(r"\b(2160p|1080p|720p|480p|4k|uhd|xxx|hevc|x264|x265|h264|web-?dl|webrip|mp4)\b", " ", s, flags=re.I)
    s = re.sub(r"(?<=\w)-(?=[A-Z])", " - ", s)
    return re.sub(r"\s+", " ", s).strip(" -")

def split_studio(text, folder=None):
    """'SisLovesMe Claire Roos' -> ('Sis Loves Me', 'Claire Roos'). Uses the folder name when it prefixes the text."""
    t = text.strip()
    for cand in [folder] if folder and folder != "_No Studio" else []:
        m = re.match(r"\s*" + r"[\s\-]*".join(map(re.escape, re.findall(r"[A-Za-z0-9]", cand))) + r"\b\s*-?\s*", t, re.I)
        if m: return (cand if re.search(r"[\s'.]", cand) else camel_split(cand)), t[m.end():].strip(" -")
    m = re.match(r"([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]+)+)\b\s*-?\s*(.*)$", t)  # CamelCase prefix
    if m: return camel_split(m.group(1)), m.group(2)
    return (camel_split(folder) if folder and folder != "_No Studio" else None), t

def score(scene, studio, date, text, aliases=()):
    """Confidence score of a ThePornDB scene vs. what the filename says. Returns (score, reasons) or (-1, ...) on a date clash."""
    site = scene.get("site") or {}
    names = {nrm(site.get("name")), nrm(site.get("short_name"))}
    for k in ("network", "parent"):  # e.g. 'MILF Body' belongs to the MYLF network
        if isinstance(site.get(k), dict): names.add(nrm(site[k].get("name")))
    names.discard("")
    s, why = 0, []
    keys = {nrm(studio)} | {nrm(a) for a in aliases}; keys.discard("")  # the filename's studio and its ThePornDB spellings
    if any(n == sk or (len(n) > 4 and len(sk) > 4 and (n in sk or sk in n)) for n in names for sk in keys): s += 3; why.append("studio")
    rd = (scene.get("date") or "")[:10]
    if date and rd:
        if date != rd: return -1, ["date clash"]
        s += 4; why.append("date")
    fw = set(words(text)); low = nrm(text)
    perfs = [p.get("name") for p in scene.get("performers") or [] if p.get("name")]
    hit = [p for p in perfs if nrm(p) and nrm(p) in low]
    if hit: s += min(4, 2 * len(hit)); why.append(f"performers {len(hit)}/{len(perfs)}")
    tw = [w for w in words(scene.get("title")) if not re.fullmatch(r"s\d+|e\d+|\d+", w)]
    if tw and set(tw) <= set(words(" ".join(perfs))): tw = []  # title is just the performer names: no extra evidence
    if tw:
        cov = sum(1 for w in tw if w in fw) / len(tw)
        pw = set(words(" ".join(perfs))); rest = [w for w in fw if w not in pw and w not in set(words(studio))]
        back = sum(1 for w in rest if w in set(tw)) / len(rest) if rest else 0  # truncated filenames: their words all in the title
        t = max(cov, back)
        if t >= .85: s += 4; why.append("title")  # >=85%: filename titles are often cut off
        elif t >= .6: s += 3; why.append(f"title {t:.0%}")
        elif t >= .34: s += 1; why.append(f"title {t:.0%}")
    return s, why

def is_confident(sv, why):
    """Needs a real studio or date agreement PLUS title/performer evidence, never studio+date alone."""
    st, dt = "studio" in why, "date" in why
    title = any(w == "title" or (w.startswith("title ") and int(w[6:-1]) >= 60) for w in why)
    some_title = any(w.startswith("title") for w in why); perf = any(w.startswith("performers") for w in why)
    m = [re.match(r"performers (\d+)/(\d+)$", w) for w in why]; m = next((x for x in m if x), None)
    all_perf = bool(m) and m.group(1) == m.group(2) and int(m.group(1)) >= 2
    if sv >= CONFIDENT and all_perf and "title" in why: return True  # every performer + the full title, even without studio
    return sv >= CONFIDENT and ((st and (title or (dt and (perf or some_title)) or (perf and some_title))) or (dt and perf and some_title))

CONFIDENT = 7   # e.g. studio + title (3+4), studio + date (3+4), studio + 2 performers + partial title
REVIEW = 5      # reported as low-confidence candidates, never applied automatically

def _fetch_all(fetch, qs, workers=6):
    """Run ThePornDB searches in parallel (each takes ~7-13 s on their side); results in the same order, None on error."""
    from concurrent.futures import ThreadPoolExecutor
    def one(q):
        try: return fetch("/scenes", **q).get("data") or []
        except Exception: return None
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(qs)))) as ex: return list(ex.map(one, qs))

def identify(token, path, folder=None, fetch=None, site_cache=None):
    """Try strategies in order; return dict(scene, score, why, strategy, confident) for the best candidate, or None.
    fetch(path, **params) does the HTTP call (injectable for tests); defaults to call() with retries."""
    fetch = fetch or (lambda p, **q: call_retry(p, token, **q))
    stem = os.path.splitext(re.split(r"[\\/]", path)[-1])[0]
    text = clean_stem(stem)
    if folder is None: folder = re.split(r"[\\/]", path)[-2] if len(re.split(r"[\\/]", path)) > 1 else None
    studio, rest = split_studio(text, folder)
    date, m = _date(rest)
    if m: rest = (rest[:m.start()] + " " + rest[m.end():]).strip()
    aliases = resolve_site(fetch, studio, site_cache) if studio else []
    best = None
    def consider(cands, strat):
        nonlocal best
        for sc in cands[:8]:
            sv, why = score(sc, studio, date, f"{studio or ''} {rest}", aliases)
            if sv >= 0 and (best is None or sv > best["score"]): best = dict(scene=sc, score=sv, why=why, strategy=strat)
    if os.path.isfile(path):
        try:
            h = oshash(path)
            if h:
                r = fetch("/scenes", hash=h, hashType="OSHASH").get("data") or []
                if r: return dict(scene=r[0], score=99, why=["fingerprint"], strategy="oshash", confident=True)
        except Exception: pass
    site_name = aliases[0] if aliases else studio
    queries = [("site-q", q) for q in site_queries(stem, text, rest)]
    queries += [("parse", dict(parse=" ".join(x for x in [site_name, date, rest] if x)))]
    if site_name and date: queries.append(("studio+date", dict(site=site_name, date=date)))
    short = " ".join(rest.split()[:7])
    if site_name and short: queries.append(("studio+text", dict(q=short, site=site_name)))
    if short: queries.append(("text", dict(q=short)))
    for (strat, q), res in zip(queries, _fetch_all(fetch, [q for _, q in queries])):
        if res is None: continue
        consider(res, strat)
        if best and best["score"] >= CONFIDENT + 2: break
    if best: best["confident"] = is_confident(best["score"], best["why"])
    return best

def site_queries(stem, text, rest):
    """Queries like ThePornDB website's scene search box (plain ?q=, no parse/direct): every term must match, so try the
    cleaned name, the raw name, the name without the studio glue, then shorter word windows (titles/performers)."""
    raw = re.sub(r"\s+", " ", SITE_JUNK.sub(" ", stem or "")).strip(" -")  # raw name minus '1280x720', 'EPORN', 'Watch on…'
    out = [text, raw, rest]
    w = [x for x in re.findall(r"[A-Za-z0-9']+", rest) if not re.fullmatch(r"\d{1,2}|\d{5,}", x)]
    for n in (5, 4, 3):
        if len(w) > n: out += [" ".join(w[:n]), " ".join(w[-n:])]
    if len(w) > 6: out.append(" ".join(w[1:5]))
    seen, res = set(), []
    for q in out:
        q = re.sub(r"\s+", " ", q or "").strip(" -")
        if len(q) >= 4 and q.lower() not in seen: seen.add(q.lower()); res.append(dict(q=q, orderBy="most_relevant"))
    return res

def candidates(token, path, query=None, limit=8, fetch=None, site_cache=None):
    """Top ThePornDB candidates for a file (or a typed query), best first: list of dict(scene, score, why, confident)."""
    fetch = fetch or (lambda p, **q: call_retry(p, token, **q))
    parts = re.split(r"[\\/]", path or "")
    stem = os.path.splitext(parts[-1])[0] if path else ""
    folder = parts[-2] if len(parts) > 1 else None
    text = clean_stem(query or stem)
    studio, rest = split_studio(text, None if query else folder)
    date, m = _date(rest)
    if m: rest = (rest[:m.start()] + " " + rest[m.end():]).strip()
    aliases = resolve_site(fetch, studio, site_cache) if studio else []
    site_name = aliases[0] if aliases else studio
    short = " ".join(rest.split()[:7])
    qs = site_queries(query or stem, text, rest) + [dict(parse=" ".join(x for x in [site_name, date, rest] if x))]
    if site_name and date: qs.append(dict(site=site_name, date=date))
    if site_name and short: qs.append(dict(q=short, site=site_name))
    if short: qs.append(dict(q=short))
    seen, out = set(), []
    for res in _fetch_all(fetch, qs):
        if res is None: continue
        for sc in res[:10]:
            k = sc.get("id") or sc.get("_id")
            if not k or k in seen: continue
            seen.add(k); sv, why = score(sc, studio, date, f"{studio or ''} {rest}", aliases)
            out.append(dict(scene=sc, score=sv, why=why, confident=sv >= 0 and is_confident(sv, why)))
    out.sort(key=lambda e: -e["score"])
    return dict(query=" ".join(x for x in [studio, date, rest] if x), results=out[:limit])

def brief(sc, images=True):
    """Small JSON-able summary of a scene for candidate pickers."""
    site = sc.get("site") or {}
    d = dict(id=str(sc.get("id") or sc.get("_id")), title=sc.get("title"), studio=site.get("name"), date=(sc.get("date") or "")[:10],
             performers=[p.get("name") for p in sc.get("performers") or [] if p.get("name")], duration=sc.get("duration"),
             url=f"https://theporndb.net/scenes/{sc.get('slug') or sc.get('id')}")
    if images: d["thumb"] = (sc.get("posters") or {}).get("small") if isinstance(sc.get("posters"), dict) else None
    if images and not d.get("thumb"): d["thumb"] = sc.get("poster") or sc.get("image")
    return d

def resolve_site(fetch, studio, cache=None):
    """ThePornDB site names matching a studio spelling (via /sites?q=), e.g. 'Sis Loves Me' -> ['Sis Loves Me']."""
    k = nrm(studio)
    if cache is not None and k in cache: return cache[k]
    out = []
    try:
        for r in (fetch("/sites", q=studio).get("data") or [])[:10]:
            for nm in (r.get("name"), r.get("short_name")):
                if nm and nrm(nm) == k: out.append(r.get("name"))
    except Exception: pass
    out = list(dict.fromkeys(out))
    if cache is not None: cache[k] = out
    return out

def call_retry(path, token, tries=4, **params):
    for i in range(tries):
        try:
            time.sleep(0.35); return call(path, token, **params)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                ra = e.headers.get("Retry-After") if e.headers else None
                time.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else 2 * (i + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if i < tries - 1: time.sleep(2 * (i + 1)); continue
            raise
