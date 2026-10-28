"""Free fallback sources for birth dates (used by birthdates.py after TMDB / ThePornDB / Wikidata / Wikipedia).

Only public pages reachable with plain HTTP (no login, no paid API), fetched slowly and politely:
one request per site every SITE_GAP seconds at most, robots.txt checked once per site, back off on 429,
and a site that answers 403 / a bot challenge is switched off (reported in the status line), never worked around.

A page only counts when its name or one of its aliases equals the person's name (or one of their aliases) AND
it is corroborated by our database: its career years overlap the years of the person's credits (library titles,
TMDB credits, ThePornDB scenes, profile career), or it names one of their titles. Otherwise it is a review candidate.

Sites not used (checked 2026-10-28):
  IAFD (iafd.com)            robots.txt: "No sucking our data ... no data" (opts out of automated data collection)
  Data18 (data18.com)        403 + captcha for scripted requests
  EGAFD (egafd.com)          Cloudflare bot challenge (403 "Just a moment")
  Euro Babe Index            no birth dates on its pages; asks spiders to stay away
  AFDB (adultfilmdatabase)   needs its search plus a 30 s crawl delay; not used yet
"""
import re, json, time, html, datetime, threading, urllib.request, urllib.parse, urllib.error, urllib.robotparser
import db

UA = "FilmTagger/1.3 (https://github.com/kylerhudson1975/film-tagger; birth-date lookup)"
SITE_GAP = 5.0  # seconds between two requests to the same site
SKIPPED = [
    dict(site="IAFD", why="robots.txt asks not to collect its data automatically"),
    dict(site="Data18", why="blocks scripted requests (403 + captcha)"),
    dict(site="EGAFD", why="Cloudflare bot challenge"),
    dict(site="Euro Babe Index", why="no birth dates; asks crawlers to stay away"),
]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september",
                                       "october", "november", "december"], 1)}
CHALLENGE = re.compile(r"cf-chl|Just a moment\.\.\.|captcha-delivery|challenge-platform|Attention Required", re.I)

class Site:
    def __init__(self, key, label, host, url_for):
        self.key, self.label, self.host, self.url_for = key, label, host, url_for
        self.lock, self.last, self.robots = threading.Lock(), 0.0, None
        self.off_until, self.off_why = 0.0, None
        self.requests = self.pages = self.filled = 0

    def state(self):
        if self.off_until > time.time(): return "paused: " + (self.off_why or "")
        return "ok"

    def _wait(self):
        with self.lock:
            w = SITE_GAP - (time.time() - self.last)
            if w > 0: time.sleep(w)
            self.last = time.time()

    def _off(self, secs, why): self.off_until, self.off_why = time.time() + secs, why

    def allowed(self, url):
        if self.robots is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait(); self.requests += 1
                with _open(f"https://{self.host}/robots.txt") as r: rp.parse(r.read().decode("utf-8", "replace").splitlines())
            except urllib.error.HTTPError as e:
                if e.code in (401, 403): self._off(86400, f"robots.txt answered {e.code}"); return False
                rp.parse([])  # no robots.txt: everything allowed
            except Exception:
                return False  # try again next time
            self.robots = rp
        return self.robots.can_fetch(UA, url)

    def get(self, url):
        """Page text, None when the page doesn't exist; raises Blocked when the site is (now) off."""
        if self.off_until > time.time(): raise Blocked(self.off_why)
        if not self.allowed(url):
            if self.off_until > time.time(): raise Blocked(self.off_why)
            raise Blocked("robots.txt disallows this page")
        for i in range(2):
            self._wait(); self.requests += 1
            try:
                with _open(url) as r:
                    if _is_search(r.geturl()): return None
                    t = r.read(3_000_000).decode("utf-8", "replace")
                if CHALLENGE.search(t[:20000]) and len(t) < 30000:
                    self._off(86400, "bot challenge page"); raise Blocked(self.off_why)
                self.pages += 1
                return t
            except urllib.error.HTTPError as e:
                if e.code in (404, 410): return None
                if e.code == 429:
                    ra = (e.headers.get("Retry-After") or "") if e.headers else ""
                    wait = min(600.0, float(ra)) if ra.replace(".", "").isdigit() else 120.0
                    if i == 0: time.sleep(wait); continue
                    self._off(3600, "rate limited (429)"); raise Blocked(self.off_why)
                if e.code in (401, 403, 451):
                    self._off(86400, f"refused ({e.code})"); raise Blocked(self.off_why)
                if i == 0 and e.code >= 500: time.sleep(30); continue
                self._off(1800, f"error {e.code}"); raise Blocked(self.off_why)
            except Blocked: raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if i == 0: time.sleep(15); continue
                self._off(1800, f"unreachable ({type(e).__name__})"); raise Blocked(self.off_why)
        return None

class Blocked(Exception): pass

class _NoSearch(urllib.request.HTTPRedirectHandler):
    """Don't follow a redirect to a site search (Babepedia sends unknown names there): treat as 'no page'."""
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if _is_search(newurl): raise urllib.error.HTTPError(newurl, 404, "redirected to search", hdrs, fp)
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)

def _is_search(u): return bool(re.search(r"/search(/|\?|$)|[?&]search=", urllib.parse.urlsplit(u).path + "?" + (urllib.parse.urlsplit(u).query or "")))

_OPENER = urllib.request.build_opener(_NoSearch)
def _open(url):
    return _OPENER.open(urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5",
                                                             "Accept-Language": "en"}), timeout=30)

def _text(h):
    h = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", html.unescape(h)).strip()

def parse_date(s):
    """'August 4, 1955' / '4th of August 1955' / '4 August 1955' / '1955-08-04' -> '1955-08-04' (or None)."""
    s = " ".join((s or "").replace(",", " ").split())
    m = re.search(r"\b(\d{4})-(\d\d)-(\d\d)\b", s)
    if m: y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})\b", s)
        if m and m.group(1).lower() in MONTHS: mo, d, y = MONTHS[m.group(1).lower()], int(m.group(2)), int(m.group(3))
        else:
            m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([A-Za-z]+)\s+(\d{4})\b", s)
            if not (m and m.group(2).lower() in MONTHS): return None
            d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
    try:
        dt = datetime.date(y, mo, d)
        return dt.isoformat() if 1880 <= y <= datetime.date.today().year - 16 else None
    except ValueError: return None

def _years(s):
    """'1976–1980' -> (1976, 1980); '2015–present' / 'Active' -> (2015, this year); '1976' -> (1976, None)."""
    m = re.search(r"\b(19\d\d|20\d\d)\s*(?:[-–—]|to)\s*(19\d\d|20\d\d|present|now|today)", s or "", re.I)
    if m: return int(m.group(1)), (int(m.group(2)) if m.group(2).isdigit() else datetime.date.today().year)
    m = re.search(r"\b(19\d\d|20\d\d)\b", s or "")
    return (int(m.group(1)), None) if m else (None, None)

def _split_names(s, sep):
    return [x.strip(" .;") for x in re.split(sep, s or "") if 2 <= len(x.strip(" .;")) <= 60]

# ---------- the sites (in the order tried) ----------
def _wiki_title(name): return urllib.parse.quote(" ".join(name.split()).replace(" ", "_"), safe="_()'.-")
def _slug(name): return re.sub(r"[^a-z0-9]+", "-", db.fold(" ".join(name.split()))).strip("-")

def parse_boobpedia(h):
    if re.search(r'wgCategories":\[[^\]]*"Disambiguation', h): return None
    m = re.search(r'wgPageName":"((?:[^"\\]|\\.)*)"', h)
    try: name = json.loads('"' + m.group(1) + '"').replace("_", " ") if m else ""
    except ValueError: name = m.group(1).replace("_", " ")
    i = h.find('class="infobox'); box = _text(h[i:h.find("</table>", i)]) if i >= 0 else ""
    bd = None
    m = re.search(r'<span class="bday">(\d{4}-\d\d-\d\d)</span>', h)
    if m: bd = parse_date(m.group(1))
    elif box:
        m = re.search(r"\bBorn\s+(.{0,40}?\d{4})", box); bd = parse_date(m.group(1)) if m else None
    m = re.search(r"Also known as\s+(.*?)\s+(?:Born|Died|Years active|Ethnicity|Nationality)\b", box)
    al = _split_names(m.group(1), r",|;| / ") if m else []
    m = re.search(r"Years active\s+(.{0,30})", box); s, e = _years(m.group(1)) if m else (None, None)
    return dict(name=name, aliases=al, birthday=bd, start=s, end=e, text=box)

def parse_freeones(h):
    t = _text(h); i = t.find("Personal information Name:")
    if i < 0: i = t.find("Name:")
    if i < 0: return None
    t = t[i:i + 3000]
    m = re.search(r"Name:\s+(.*?)\s+(?:Aliases:|Official website:)", t); name = m.group(1) if m else ""
    m = re.search(r"Aliases:\s+(.*?)\s+(?:Official website:|Feature dancer:|Date of birth:)", t)
    al = _split_names(m.group(1), r"\s,\s|,") if m else []
    m = re.search(r"Date of birth:\s+(.{0,40}?\d{4})", t); bd = parse_date(m.group(1)) if m else None
    ms, me = re.search(r"Career start:\s+(\d{4})", t), re.search(r"Career end:\s+(\d{4}|Present|N/A)", t, re.I)
    s = int(ms.group(1)) if ms else None
    e = (int(me.group(1)) if me and me.group(1).isdigit() else (datetime.date.today().year if s and re.search(r"Career status:\s+Active", t) else None))
    return dict(name=name, aliases=al, birthday=bd, start=s, end=e, text=t)

def parse_babepedia(h):
    m = re.search(r'<h1 id="babename">([^<]+)', h); name = html.unescape(m.group(1)).strip() if m else ""
    if not name: return None
    t = _text(h)
    m = re.search(r"Also known as:\s+(.*?)\s+(?:" + re.escape(name) + r" alias list|Age:|Born:)", t)
    al = _split_names(m.group(1), r"\s-\s") if m else []
    m = re.search(r"\bBorn:\s+(.{0,50}?\d{4})", t); bd = parse_date(m.group(1)) if m else None
    m = re.search(r"Years active:\s+(.{0,30})", t); s, e = _years(m.group(1)) if m else (None, None)
    return dict(name=name, aliases=al, birthday=bd, start=s, end=e, text=t[:6000])

SITES = [
    (Site("boobpedia", "Boobpedia", "www.boobpedia.com", lambda n: f"https://www.boobpedia.com/boobs/{_wiki_title(n)}"), parse_boobpedia),
    (Site("freeones", "FreeOnes", "www.freeones.com", lambda n: f"https://www.freeones.com/{_slug(n)}/bio"), parse_freeones),
    (Site("babepedia", "Babepedia", "www.babepedia.com", lambda n: f"https://www.babepedia.com/babe/{_wiki_title(n)}"), parse_babepedia),
]
LABEL = {s.key: s.label for s, _ in SITES}

def status():
    return [dict(key=s.key, label=s.label, state=s.state(), requests=s.requests, pages=s.pages, filled=s.filled) for s, _ in SITES]

# ---------- matching ----------
def check(p, names, years, titles, wd_year=None):
    """(confident: bool, why) for a parsed page against our names / credit years / titles."""
    mine = {db.fold(" ".join(n.split())) for n in names}
    theirs = {db.fold(" ".join(n.split())) for n in [p["name"]] + p["aliases"] if n}
    if not mine & theirs: return False, "name not on the page"
    by = int(p["birthday"][:4])
    ys = sorted(y for y in years if y)
    if wd_year and wd_year != by: return False, f"Wikidata says born {wd_year}"
    single = all(len(n.split()) < 2 for n in mine & theirs)  # 'Pearl': too many strangers share one-word names
    s, e = p["start"], p["end"]
    if s and ys:
        e2 = e or s + 3  # only a start year: allow a short window
        hit = [y for y in ys if s - 1 <= y <= e2 + 1]
        if hit:
            if not 16 <= hit[0] - by <= 80: return False, f"born {by} doesn't fit credits in {hit[0]}"
            if single: return False, f"one-word name (career {s}–{e or '?'} fits)"
            return True, f"career {s}–{e or '?'} fits credits in {hit[0]}" + (f"–{hit[-1]}" if hit[-1] != hit[0] else "")
    low = db.fold(p.get("text") or "")
    for t in titles:
        ft = db.fold(t)
        if len(ft) >= 10 and len(ft.split()) >= 2 and ft in low and not single: return True, f"page names '{t}'"
    if not ys: return False, "no credit years here to compare"
    return False, (f"career {s}–{e or '?'} vs credits {ys[0]}–{ys[-1]}" if s else "page has no career years")

def lookup(names, years, titles, wd_year=None, urls=None):
    """Try the sites in order. -> dict(hit=dict|None, cands=[...], tried=[labels], skipped={label: why})."""
    out = dict(hit=None, cands=[], tried=[], skipped={})
    urls = urls or {}
    for site, parse in SITES:
        if site.off_until > time.time(): out["skipped"][site.label] = site.off_why; continue
        seen = set()
        tries = ([urls[site.key]] if urls.get(site.key) else []) + [site.url_for(n) for n in names[:3]]
        for url in tries:
            if url in seen: continue
            seen.add(url)
            try: h = site.get(url)
            except Blocked as b: out["skipped"][site.label] = str(b); break
            if site.label not in out["tried"]: out["tried"].append(site.label)
            if not h: continue
            try: p = parse(h)
            except Exception: p = None
            if not p or not p.get("birthday"): continue
            ok, why = check(p, names, years, titles, wd_year)
            c = dict(site=site.key, label=site.label, url=url, birthday=p["birthday"], name=p["name"], why=why,
                     career=(f"{p['start']}–{p['end'] or ''}" if p["start"] else None))
            if ok:
                conflict = [x for x in out["cands"] if x["birthday"] != p["birthday"] and x["why"] != "name not on the page"]
                if conflict: out["cands"].append(c); return out
                out["hit"] = c; site.filled += 1; return out
            if why != "name not on the page": out["cands"].append(c)
            break  # this site had a page for the name: don't try more spellings
    return out
