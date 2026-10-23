"""Find Gallery: search ViperGirls (as a guest, no login) for a scene's photo set and download it with gallery-dl.

gallery-dl (GPL-2.0, https://github.com/mikf/gallery-dl) is used only as a separate program via subprocess
(`python -m gallery_dl`); it is never imported. Search strategy (thread titles are unreliable, so the scene title is
never used): performer keyword(s) limited to the studio's prefix tag, else performers + studio as plain text.
Results are ranked by how many of the scene's performers and its studio appear in the thread title, with a small
boost when a date in the title is close to the release date."""
import os, re, sys, hashlib, json, time, html, threading, subprocess, tempfile, urllib.request, urllib.parse, datetime
import db, logging
log = logging.getLogger("filmtagger.findgallery")

BASE = "https://vipergirls.to/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FilmTagger"
MIN_GAP_S = 20          # at least 20 s between searches (be polite to the forum)
CACHE_S = 3600          # cache results for an hour
PREFIX_TTL_S = 7 * 86400
MIN_FILE_BYTES = 10 * 1024
PLACEHOLDER_SIZES = {8090, 9584, 503, 2142, 8183}  # 8183 = Imagetwist 'deleted or moved' image (HEAD Content-Length)  # known 'image removed' / placeholder sizes (anything < 10 KB is dropped anyway)
DL_TIMEOUT_S = 45 * 60
HTTP_TIMEOUT_S = 20     # every request to the forum gives up after this
LOCK_WAIT_S = 30        # a search waits at most this long for another search to finish
MAX_STEPS = 7           # most forum searches one Find Gallery search may make
SEARCH_BUDGET_S = 150   # a Find Gallery search stops starting new steps after this
SEARCH = {"step": "", "started": 0.0, "n": 0}  # progress of the current search, shown by the panel

_LOCK = threading.Lock(); _STATE = {"last": 0.0}; _CACHE = {}; _PREFIX = {"at": 0, "list": []}

def http(url, data=None, timeout=HTTP_TIMEOUT_S):
    """GET (or POST form data) -> text. Patched in tests."""
    body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def key(s): return db.studio_key(s)

# ---------- prefixes (from the Advanced Search form) ----------
def parse_prefixes(page):
    i = page.find('name="prefixchoice[]"')
    if i < 0: return []
    j = page.find("</select>", i)
    return [(html.unescape(v), html.unescape(t).strip()) for v, t in re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', page[i:j]) if v]

FORUM_NAME = "Adult Photo Sets"
FORUM_FALLBACK = "235"  # its id on vipergirls.to (Oct 2026), used if the form can't be parsed

def parse_forum_id(page, name=FORUM_NAME):
    """Forum id of the option labelled exactly `name` in the search form's forum list, or None."""
    for m in re.finditer(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)<', page or ""):
        if html.unescape(m.group(2)).strip().lower() == name.lower(): return m.group(1)
    return None

def forum_id():
    return _PREFIX.get("forum") or FORUM_FALLBACK

def prefixes(cache_file=None):
    if _PREFIX["list"] and time.time() - _PREFIX["at"] < PREFIX_TTL_S: return _PREFIX["list"]
    if cache_file and os.path.exists(cache_file):
        try:
            d = json.load(open(cache_file, encoding="utf-8"))
            if time.time() - d["at"] < PREFIX_TTL_S and d.get("forum"):
                _PREFIX.update(at=d["at"], list=[tuple(x) for x in d["list"]], forum=str(d["forum"])); return _PREFIX["list"]
        except Exception: pass
    page = http(BASE + "search.php?search_type=1")
    lst = parse_prefixes(page); fid = parse_forum_id(page)
    if not fid: log.warning("Find Gallery: 'Adult Photo Sets' forum not found in the search form; using id %s", FORUM_FALLBACK)
    _PREFIX["forum"] = fid or FORUM_FALLBACK
    if lst:
        _PREFIX.update(at=time.time(), list=lst)
        if cache_file:
            try: json.dump({"at": _PREFIX["at"], "list": lst, "forum": _PREFIX["forum"]}, open(cache_file, "w", encoding="utf-8"))
            except OSError: pass
    return lst

def match_prefix(studio_names, plist):
    """Prefix value whose label or value matches the studio or an alias (spaces, case, punctuation and .com ignored)."""
    want = {key(n) for n in studio_names if n} - {""}
    want |= {re.sub(r"(com|net|org)$", "", k) for k in want}
    for v, label in plist:
        ks = {key(label), key(v), re.sub(r"(com|net|org)$", "", key(v)), re.sub(r"(com|net|org)$", "", key(label))}
        if ks & want: return v
    return None

# ---------- search ----------
def parse_results(page):
    """Search results page (posts or threads view) -> [{thread_id, title, url, post_url}] in page order, one per thread."""
    out, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(threads/(\d+)[^"]*)"[^>]*title="([^"]*)"', page):
        tid = m.group(2)
        if tid in seen: continue
        seen.add(tid)
        pm = re.search(r'href="(threads/' + tid + r'[^"]*[?&](?:amp;)?p=(\d+)[^"]*)"', page[m.end():m.end() + 6000])
        out.append(dict(thread_id=tid, title=html.unescape(m.group(3)).strip(), url=BASE + "threads/" + tid,
                        post_url=post_link(tid, pm.group(2)) if pm else None))
    for m in re.finditer(r'<a[^>]+href="(threads/(\d+)[^"]*)"[^>]*id="thread_title_\d+"[^>]*>([^<]*)</a>', page):  # threads view
        tid = m.group(2)
        if tid not in seen:
            seen.add(tid); out.append(dict(thread_id=tid, title=html.unescape(m.group(3)).strip(), url=BASE + "threads/" + tid, post_url=None))
    return out

def guess_names(text, studio_names=()):
    """Unlinked scene: 'Elena Koshka Aubrey Sinclair' (title/cast/file name) -> ['Elena Koshka', 'Aubrey Sinclair'].
    Drops studio words and dates/numbers, then pairs words up (first + last name)."""
    t = re.sub(r"[._]+", " ", text or "")
    for n in sorted(studio_names, key=len, reverse=True):
        for v in {n, n.replace(" ", "")}:
            t = re.sub(re.escape(v), " ", t, flags=re.I)
    w = [x for x in re.split(r"[\s,&+/()\[\]-]+", t) if x and not re.search(r"\d", x) and x.lower() not in ("and", "with", "x", "mp4")]
    if len(w) >= 4 and len(w) % 2 == 0: return [" ".join(w[i:i + 2]) for i in range(0, len(w), 2)]
    return [" ".join(w)] if w else []

def form(query, prefix=None, all_forums=False):
    d = {"do": "process", "securitytoken": "guest", "searchfromtype": "vBForum:Post", "contenttypeid": "1", "query": query,
         "titleonly": "0", "showposts": "1", "childforums": "1", "forumchoice[]": [""] if all_forums else [forum_id()], "sortby": "dateline", "order": "descending",
         "searchdate": "0", "beforeafter": "after", "dosearch": "Search Now"}
    if prefix: d["prefixchoice[]"] = [prefix]
    return d

def wait_s():
    return max(0.0, MIN_GAP_S - (time.time() - _STATE["last"]))

def search(query, prefix=None, block=False, all_forums=False):
    """One forum search (cached). Raises Busy(seconds) if another search ran less than 20 s ago (unless block)."""
    ck = (query.strip().lower(), prefix or "", "" if all_forums else forum_id())  # forum is part of the key
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < CACHE_S: return hit[1], True
    if not _LOCK.acquire(timeout=LOCK_WAIT_S if block else 0.5):  # one search at a time, never wait forever
        raise Busy(max(wait_s(), 5))
    try:
        w = wait_s()
        if w > 0:
            if not block: raise Busy(w)
            time.sleep(w)
        try: page = http(BASE + "search.php?do=process", form(query, prefix, all_forums), timeout=HTTP_TIMEOUT_S)
        finally: _STATE["last"] = time.time()
    finally:
        _LOCK.release()
    res = parse_results(page)
    _CACHE[ck] = (time.time(), res)
    return res, False

class Busy(Exception):
    def __init__(self, s): super().__init__(f"wait {s:.0f}s"); self.seconds = s

# ---------- ranking ----------
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
def title_dates(t):
    out = []
    for y, mo, d in re.findall(r"\b((?:19|20)\d\d)[.\-/ ](\d{1,2})[.\-/ ](\d{1,2})\b", t): out.append((int(y), int(mo), int(d)))
    for d, mo, y in re.findall(r"\b(\d{1,2})[.\-/](\d{1,2})[.\-/]((?:19|20)\d\d)\b", t): out.append((int(y), int(mo), int(d)))
    for mo, d, y in re.findall(r"\b([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? ((?:19|20)\d\d)\b", t):
        if mo.lower() in MONTHS: out.append((int(y), MONTHS[mo.lower()], int(d)))
    res = []
    for y, mo, d in out:
        try: res.append(datetime.date(y, mo, d))
        except ValueError: pass
    return res

def rank(results, performers, studio_names, release_date=None, prefix_used=False):
    """Score = performers found + 1 for the studio (or alias / prefix search) + up to 0.5 when a title date is close."""
    rd = None
    try: rd = datetime.date.fromisoformat(release_date[:10]) if release_date else None
    except ValueError: pass
    groups = [[n] if isinstance(n, str) else list(n) for n in performers]  # each performer: name, then aliases
    groups = [[x for x in g if x and x.strip()] for g in groups]; groups = [g for g in groups if g]
    sk = {key(s) for s in studio_names if s} - {""}
    for r in results:
        t = db.fold(r["title"]); tk = key(r["title"])
        found = []
        for g in groups:  # an alias counts as that performer
            hit = next((n for n in g if db.fold(" ".join(n.split())) in t or key(n) in tk), None)
            if hit: found.append(hit)
        st = bool(prefix_used) or any(k and k in tk for k in sk)
        boost = 0.0
        if rd:
            ds = [abs((d - rd).days) for d in title_dates(r["title"])]
            if ds: boost = 0.5 if min(ds) == 0 else 0.3 if min(ds) <= 7 else 0.1 if min(ds) <= 31 else 0.0
        r.update(score=round(len(found) + (1 if st else 0) + boost, 2), performers_found=len(found), matched=found, studio_found=st, date_boost=boost)
    return sorted(results, key=lambda r: -r["score"])

# ---------- URLs ----------
def post_link(tid, pid):
    """The post form gallery-dl's vipergirls extractor accepts: threads/<id>?p=<pid>#post<pid>."""
    return f"{BASE}threads/{tid}?p={pid}#post{pid}"

def normalize_url(u):
    """A pasted ViperGirls thread or post URL -> URL gallery-dl accepts (session code etc. dropped), or None.
    threads/<id>[-slug][?...] -> threads/<id>;  ...?p=<pid> or #post<pid> -> threads/<id>?p=<pid>#post<pid>.
    Bare posts/<pid> links carry no thread id, which gallery-dl needs, so they are refused."""
    u = (u or "").strip().replace("&amp;", "&")
    m = re.match(r"^(?:https?://)?(?:www\.)?vipergirls\.to/threads/(\d+)(?:-[^\s/?#]*)?/?(?:page\d+)?(\?[^\s#]*)?(#post(\d+))?$", u, re.I)
    if not m: return None
    q = urllib.parse.parse_qs((m.group(2) or "")[1:])
    pid = (q.get("p") or [None])[0] or m.group(4)
    if pid and pid.isdigit(): return post_link(m.group(1), pid)  # one post = one photo set
    return BASE + "threads/" + m.group(1)

# ---------- learning from a hand-pasted thread URL ----------
def thread_url(u):
    """Normalized thread/post URL -> the thread page URL (no post id), or None."""
    m = re.match(r"(https://vipergirls\.to/threads/\d+)", u or "")
    return m.group(1) if m else None

def thread_info(page):
    """Thread page -> dict(title, slug, forums) (text only). title is the thread title as shown (prefix included);
    slug is the URL slug (vBulletin leaves the prefix out of it); forums are the breadcrumb forum names. None if unreadable."""
    page = page or ""
    m = re.search(r'<span class="threadtitle"><a href="threads/\d+-?([^"?#]*)[^"]*"[^>]*>([^<]*)</a>', page)
    if m: slug, title = m.group(1), m.group(2)
    else:
        t = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        if not t: return None
        slug, title = "", t.group(1)
    title = " ".join(html.unescape(title).split())
    if not title: return None
    forums = [" ".join(html.unescape(x).split()) for x in re.findall(r'<li class="navbit"><a href="forums/[^"]*">([^<]*)</a>', page)]
    return dict(title=title, slug=urllib.parse.unquote(slug), forums=forums)

def title_prefix(title, slug, plist):
    """The forum prefix a thread was posted under -> (value, label) or None. The prefix is the title's leading text that
    the slug leaves out ('BadMILFS JC Wilds, ...' vs slug 'JC-Wilds-...'); else the longest prefix label the title starts with."""
    words = (title or "").split(); sk = key(slug.replace("-", " "))[:12]
    if sk:
        for i in range(1, min(len(words), 6)):
            if key(" ".join(words[i:])).startswith(sk):
                v = match_prefix([" ".join(words[:i])], plist)
                if v: return v, dict(plist).get(v, v)
                break
    tk = key(title)
    for v, label in sorted(plist, key=lambda x: -len(key(x[1]))):
        if key(label) and tk.startswith(key(label)): return v, label
    return None

def names_in_title(title, performers):
    """True if any performer (or alias) appears in the title, False if none do, None if the scene has no performers."""
    groups = [[n] if isinstance(n, str) else list(n) for n in performers]
    groups = [[x for x in g if x and x.strip()] for g in groups]; groups = [g for g in groups if g]
    if not groups: return None
    t, tk = db.fold(title), key(title)
    return any(db.fold(" ".join(n.split())) in t or key(n) in tk for g in groups for n in g)

def learn(page, studio, performers, plist):
    """Rule learned from a hand-pasted thread page for `studio` -> dict(prefix, prefix_label, names_in_title, notes) or None.
    Only threads in Adult Photo Sets (or its subforums) count, since searches only look there. Nothing here builds a search."""
    info = thread_info(page)
    if not info or not studio: return None
    if info["forums"] and not any(f.lower() == FORUM_NAME.lower() for f in info["forums"]): return None
    pre = title_prefix(info["title"], info["slug"], plist)
    nit = names_in_title(info["title"], performers)
    if not pre and nit is not False: return None
    notes = []
    if pre: notes.append(f"{studio} threads are posted under prefix '{pre[1]}'")
    if nit is False: notes.append("thread titles leave performer names out, so searches lean on the prefix + last names and aliases")
    return dict(prefix=pre[0] if pre else None, prefix_label=pre[1] if pre else None, names_in_title=nit,
                notes="; ".join(notes)[:1].upper() + "; ".join(notes)[1:] + ".")

# ---------- download (gallery-dl as a separate process) ----------
DL = {"running": False}

def gallery_dl_cmd(url, dest):
    return [sys.executable, "-m", "gallery_dl", "--no-mtime", "-D", dest, url]

DNS_FAIL_N = 2  # this many 'Failed to resolve' lines for one host = that host is gone; stop and skip it
DNS_RE = re.compile(r"Failed to resolve '?([A-Za-z0-9.-]+)")

def dns_failed_hosts(path, n=DNS_FAIL_N):
    """Hosts gallery-dl could not resolve at least n times (text only)."""
    try:
        with open(path, "rb") as f: t = f.read().decode("utf-8", "replace")
    except (OSError, TypeError): return set()
    c = {}
    for h in DNS_RE.findall(t): c[h.lower()] = c.get(h.lower(), 0) + 1
    return {h for h, k in c.items() if k >= n}

def resolves(host, _cache={}):
    if host not in _cache:
        import socket
        try: socket.getaddrinfo(host, 443); _cache[host] = True
        except OSError: _cache[host] = False
    return _cache[host]

def list_urls(url, run=None, timeout=300):
    """`gallery-dl -g` (list only, nothing saved) -> the file/page URLs of a thread."""
    run = run or subprocess.run
    r = run([sys.executable, "-m", "gallery_dl", "-g", url], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return [x.strip() for x in (r.stdout or "").splitlines() if x.strip().startswith("http")]

def live_urls(urls, bad=(), ok=resolves):
    """Drop URLs whose host is in bad or does not resolve (e.g. dead promo images embedded next to the real set)."""
    out, dead = [], set()
    for u in urls:
        h = (urllib.parse.urlsplit(u).hostname or "").lower()
        if h in bad or not ok(h): dead.add(h)
        else: out.append(u)
    return out, dead

def dead_host_msg(hosts):
    return f"This gallery's images are hosted on a site that no longer exists ({', '.join(sorted(hosts))}). Try another result."

def input_file_cmd(path, dest):
    return [sys.executable, "-m", "gallery_dl", "--no-mtime", "-D", dest, "-i", path]

POST_RE = re.compile(r'<div id="post_message_(\d+)">(.*?)</blockquote>', re.S)
LINK_RE = re.compile(r'<a href="(https?://[^"]+)"[^>]*>\s*<img\s', re.I)

def parse_posts(page):
    """Thread page -> [(post id, [image-host page links])] for posts with linked thumbnails, in order.
    Only <a href> around an <img> counts (the host page, which gallery-dl resolves to the full-size image); bare
    <img src> (promo/banner pictures) and forum links are left out; repeated links are kept once."""
    out = []
    for pid, body in POST_RE.findall(page or ""):
        seen, links = set(), []
        for u in LINK_RE.findall(body):
            u = html.unescape(u)
            if "vipergirls.to" in u or u in seen: continue
            seen.add(u); links.append(u)
        if links: out.append((pid, links))
    return out

def set_links(url):
    """(main links, alternate links) for a thread/post URL: the first image post (or the linked post) is the set;
    a later post with the same number of images is the same set uploaded again (thread 7285707: 75 + 75), used only to
    fill photos that fail in the main one, by position. ([], []) when the page can't be read."""
    m = re.match(r"(https://vipergirls\.to/threads/\d+)(?:\?p=(\d+))?", url or "")
    if not m: return [], []
    try: posts = parse_posts(http(m.group(1) + (f"?p={m.group(2)}" if m.group(2) else "")))
    except Exception: return [], []
    if not posts: return [], []
    i = next((k for k, (pid, _) in enumerate(posts) if pid == m.group(2)), 0)
    main = posts[i][1]
    # match by the set's main host (thread 7285707: post 1 = 75 vipr.im + 1 imgbox cover, post 2 = 75 vipr.im)
    host = lambda u: urllib.parse.urlsplit(u).hostname or ""
    hosts = [host(u) for u in main]; top = max(set(hosts), key=hosts.count)
    mains = [u for u in main if host(u) == top]
    other = next((l for pid, l in posts[i + 1:] if len(l) >= len(mains) and sum(host(u) == host(l[0]) for u in l) == len(mains)), None)
    if not other: return main, []
    oth = [u for u in other if host(u) == host(other[0])]; it = iter(oth)
    alt = [next(it) if host(u) == top else "" for u in main]  # same position in the set; "" = no counterpart
    return main, alt

def links_cmd(path, dest, err, slow=False):
    return [sys.executable, "-m", "gallery_dl", "--no-mtime", "-D", dest, "-e", err] + \
        (["--sleep-request", RETRY_SLEEP_S, "-R", "2", "--http-timeout", "20"] if slow else []) + ["-i", path]

def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f: return [x.strip() for x in f if x.strip()]
    except OSError: return []

def count_files(d):
    try: return sum(1 for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)) and not n.endswith((".part", ".tmp")))
    except OSError: return 0

def _size(p):
    """File size, or None if it is gone/unreadable (gallery-dl renames 'x.jpg.part' -> 'x.jpg' while we look)."""
    try: return os.path.getsize(p) if os.path.isfile(p) else None
    except OSError: return None

DUP_N = 3  # this many byte-identical files in one set = a host placeholder, not photos

def placeholders(d):
    """Files in d that are placeholders, by name/size/content only (never opened as images):
    under 10 KB, a known 'removed' size, .part/.tmp leftovers, or DUP_N+ byte-identical copies."""
    out, by_size = [], {}
    try: names = os.listdir(d)
    except OSError: return []
    for x in names:
        p = os.path.join(d, x)
        sz = _size(p)
        if sz is None: continue
        if sz < MIN_FILE_BYTES or sz in PLACEHOLDER_SIZES or x.endswith((".part", ".tmp")): out.append(p)
        else: by_size.setdefault(sz, []).append(p)
    for group in by_size.values():
        if len(group) < DUP_N: continue
        by_hash = {}
        for p in group:
            try:
                with open(p, "rb") as f: by_hash.setdefault(hashlib.sha256(f.read()).hexdigest(), []).append(p)
            except OSError: pass
        for same in by_hash.values():
            if len(same) >= DUP_N: out += same
    return out

def cleanup(d):
    """Remove placeholders (see placeholders()). Returns files removed."""
    n = 0
    for p in placeholders(d):
        try: os.remove(p); n += 1
        except OSError: pass
    return n

DEAD_SET_N = 10  # this many 'not found' errors with nothing saved = the set is gone from the host
MISSING_MSG = "The image host deleted this gallery's images (it reports them missing or serves 'deleted' placeholders). Nothing was kept. Try another result."

def missing_count(path):
    """How many 'image not found' errors gallery-dl has logged (text only)."""
    try:
        with open(path, "rb") as f: t = f.read().decode("utf-8", "replace")
        return len(re.findall(r"NotFoundError|could not be found|404 Not Found", t))
    except (OSError, TypeError): return 0

FAIL_RE = re.compile(r"\]\[error\] ")
RETRY_PASSES = 2       # extra passes over the same links when some images failed but others downloaded
RETRY_SLEEP_S = "1.0"  # gallery-dl --sleep-request on retry passes (vipr.im answers 'not found' when hit fast)

def failed_count(path):
    """How many images gallery-dl reported as failed ([error] lines: not found, HTTP error, timeout). Text only."""
    try:
        with open(path, "rb") as f: return len(FAIL_RE.findall(f.read().decode("utf-8", "replace")))
    except (OSError, TypeError): return 0

def expected_count(url):
    """Photo count from the thread title ('... 75x 1620x1080 ...'), or None. One guest page fetch; patched in tests."""
    m = re.match(r"(https://vipergirls\.to/threads/\d+)", url or "")
    if not m: return None
    try: t = re.search(r"<title>(.*?)</title>", http(m.group(1)), re.S | re.I)
    except Exception: return None
    n = re.search(r"(?<![\dx])(\d{1,4})\s*x(?!\d)", html.unescape(t.group(1))) if t else None
    return int(n.group(1)) if n else None

def failed_msg(n, log_path):
    why = "image removed from host" if missing_count(log_path) >= n else "image removed or host error"
    return f"{n} failed ({why})"

def log_tail(path, n=300):
    """Last non-empty line of gallery-dl's log (text only)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 4096)); lines = f.read().decode("utf-8", "replace").strip().splitlines()
        return lines[-1][:n] if lines else ""
    except OSError: return ""

def run_download(url, dest, on_done=None, popen=None, timeout=DL_TIMEOUT_S):
    popen = popen or subprocess.Popen; log = None; log_path = ""; p = None
    os.makedirs(dest, exist_ok=True)
    DL.update(running=True, url=url, dest=dest, files=count_files(dest), error=None, cancel=False, started_at=time.time(), removed=0, rc=None, dead=set(), passes=0, failed=0, expected=None)
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # gallery-dl's messages go to a log file, never a pipe: an unread pipe fills up (~4 KB on Windows) and
        # gallery-dl then blocks forever writing a warning, so the download stalls at 0 files.
        log_path = os.path.join(tempfile.gettempdir(), "filmtagger_gallerydl.log")
        # A search hit often points at a reply post with no images (the photo set is in the thread's first post),
        # so if a post link yields nothing, retry with the whole thread.
        tm = re.match(r"(https://vipergirls\.to/threads/\d+)\?p=\d+", url)
        tries = [url] + ([tm.group(1)] if tm else [])
        # Link mode: download the set's host-page links ourselves (one gallery-dl run with a URL file), so only the
        # links that fail are retried and a set posted twice is downloaded once. Falls back to the whole URL.
        main, alt = set_links(url) if url.startswith(BASE) else ([], [])
        tmpd = tempfile.gettempdir(); LK = {"main": main, "alt": alt, "todo": list(range(len(main))), "used_alt": False}
        if main:
            DL["expected"] = len(main); tries = ["&"] + tries
        def write_links(urls, name):
            lf = os.path.join(tmpd, name)
            with open(lf, "w", encoding="utf-8") as f: f.write("\n".join(urls) + "\n")
            return lf
        for k, u in enumerate(tries):
            if k and not u.startswith(("@", "%", "&")): DL.update(url=u, last_msg="That post has no images; trying the whole thread")
            log = open(log_path, "wb"); DL["log"] = log_path
            err = os.path.join(tmpd, "filmtagger_gallerydl_failed.txt")
            if u.startswith("&"):  # link mode: LK['todo'] = positions still to get; '&&' = from the alternate post
                src = LK["alt"] if u.startswith("&&") else LK["main"]
                urls = [src[i] for i in LK["todo"] if src[i]]
                if not urls: break
                try: os.remove(err)
                except OSError: pass
                cmd = links_cmd(write_links(urls, "filmtagger_gallerydl_urls.txt"), dest, err, slow=LK.get("passes_done", 0) > 0)
                LK["passes_done"] = LK.get("passes_done", 0) + 1
            else:
                uu = u.lstrip("%"); cmd = input_file_cmd(uu[1:], dest) if uu.startswith("@") else gallery_dl_cmd(uu, dest)
                if u.startswith("%"): cmd = cmd[:-1] + ["--sleep-request", RETRY_SLEEP_S] + cmd[-1:]
            p = popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, creationflags=flags)
            DL["pid"] = getattr(p, "pid", None)
            while p.poll() is None:
                if DL["cancel"]: p.terminate(); DL["error"] = "cancelled"; break
                try:  # watching the folder must never end the download: a file can vanish/rename between list and stat
                    DL["files"] = count_files(dest); DL["last_msg"] = log_tail(log_path)
                    ph = len(placeholders(dest)) if DL["files"] >= DEAD_SET_N else 0
                except OSError as e:
                    log_w = globals()["log"]; log_w.warning("Find Gallery: folder check skipped: %s", type(e).__name__)
                    time.sleep(1); continue
                if DL["files"] == 0 and missing_count(log_path) >= DEAD_SET_N:  # dead set: stop early
                    p.terminate(); DL["error"] = MISSING_MSG; break
                if DL["files"] >= DEAD_SET_N and ph >= DL["files"]:  # only placeholder images so far
                    p.terminate(); DL["error"] = MISSING_MSG; break
                if DL["files"] == 0 and not str(u).startswith(("@", "&")) and dns_failed_hosts(log_path):  # dead host: stop, skip it
                    p.terminate(); DL["dead"] = dns_failed_hosts(log_path); break
                if time.time() - DL["started_at"] > timeout: p.terminate(); DL["error"] = "timed out"; break
                time.sleep(1)
            try: p.wait(timeout=20)
            except Exception:
                try: p.kill()
                except Exception: pass
            DL["rc"] = p.returncode
            log.close(); log = None
            if u.startswith("&") and not DL["error"] and not DL["cancel"]:
                src = LK["alt"] if u.startswith("&&") else LK["main"]
                before = len(LK["todo"])
                bad = set(read_lines(err)); LK["todo"] = [i for i in LK["todo"] if src[i] in bad or not src[i]]
                stuck = LK.get("passes_done", 0) > 1 and len(LK["todo"]) == before  # a slow retry got nothing back
                if not LK["todo"]: break
                if count_files(dest) == 0 and not u.startswith("&&"):  # nothing at all from the links: old way
                    DL["expected"] = None; continue
                if u.count("&") == 1 and LK.get("passes_done", 0) < 1 + RETRY_PASSES and not stuck:
                    DL["passes"] = LK["passes_done"]
                    DL["last_msg"] = f"{len(LK['todo'])} images failed; retrying just those (pass {DL['passes']})"
                    tries.insert(k + 1, "&"); continue
                if LK["alt"] and not u.startswith("&&"):
                    DL["last_msg"] = f"{len(LK['todo'])} images failed; trying the same photos from the thread's other upload"
                    tries.insert(k + 1, "&&"); continue
                break
            # gallery-dl exits non-zero when any image failed; that is only an error when nothing was saved
            if p.returncode not in (0, None) and not DL["error"] and not count_files(dest) and not (DL.get("dead") and not u.startswith("@")):
                DL["error"] = log_tail(log_path) or f"gallery-dl exit code {p.returncode}"
            if DL.get("dead") and not DL["error"] and not str(u).startswith("@"):
                # Posts often embed promo images from a host that is gone (thread 2358460: images.passionhdfan.com,
                # NXDOMAIN) before the real set (imx.to). gallery-dl retries each dead image, so list the URLs, drop
                # the dead hosts, and download the rest from a URL file.
                DL["last_msg"] = "Skipping dead host " + ", ".join(sorted(DL["dead"]))
                try: keep, dead = live_urls(list_urls(u), DL["dead"])
                except Exception: keep, dead = [], set()
                DL["dead"] = set(DL["dead"]) | dead
                if not keep: DL["error"] = dead_host_msg(DL["dead"]); break
                lf = os.path.join(tempfile.gettempdir(), "filmtagger_gallerydl_urls.txt")
                with open(lf, "w", encoding="utf-8") as f: f.write("\n".join(keep) + "\n")
                tries.insert(k + 1, "@" + lf); continue
            # Some images failed but others downloaded: gallery-dl already went on past each error, but the host often
            # answers 'not found' only because it was hit fast (thread 7285707: 37 of 75 vipr.im pages). Go over the
            # same links again, slower; files already saved are skipped by gallery-dl.
            if DL.get("expected") is None and count_files(dest) and failed_count(log_path): DL["expected"] = expected_count(url) or 0
            if not DL["error"] and not DL["cancel"] and count_files(dest) and failed_count(log_path) and \
                    not (DL.get("expected") and count_files(dest) >= DL["expected"]) and \
                    DL.get("passes", 0) < RETRY_PASSES:
                DL["passes"] = DL.get("passes", 0) + 1
                DL["last_msg"] = f"{failed_count(log_path)} images failed; retrying them (pass {DL['passes']})"
                tries.insert(k + 1, "%" + u.lstrip("%")); continue
            if DL["error"] or DL["cancel"] or count_files(dest) or missing_count(log_path): break
    except Exception as e:
        DL["error"] = f"{type(e).__name__}: {e}"
        # never leave gallery-dl running on its own (it would keep writing while cleanup() removes its .part files)
        try:
            if p and p.poll() is None: p.terminate(); p.wait(timeout=20)
        except Exception: pass
    finally:
        try: log and log.close()
        except Exception: pass
        DL["removed"] = cleanup(dest); DL["files"] = count_files(dest)
        if main and DL["files"] and not DL.get("error") and DL.get("expected"):  # link mode: exact unique count
            DL["failed"] = min(len(LK["todo"]), max(0, len(main) - DL["files"]))
            t = expected_count(url) if DL["failed"] else None  # title photo count, when given, caps it (a cover link is extra)
            if t: DL["failed"] = min(DL["failed"], max(0, t - DL["files"]))
            if DL["failed"]: DL["error"] = f"{DL['failed']} failed (image removed from host)"
        elif DL["files"] and not DL.get("error") and failed_count(log_path):  # partial set: say how many failed
            # threads often post the set twice (two hosts), so a failed link may be a photo already saved from the
            # other post: with a photo count in the title, failed = expected - kept
            n = failed_count(log_path); e = DL.get("expected") or 0
            DL["failed"] = max(0, e - DL["files"]) if e else n
            if DL["failed"]: DL["error"] = failed_msg(DL["failed"], log_path) if not e else f"{DL['failed']} failed (image removed from host)"
        if DL["files"] == 0 and DL.get("dead") and not DL.get("error"): DL["error"] = dead_host_msg(DL["dead"])
        if DL["files"] == 0 and DL.get("error") != "cancelled" and not (DL.get("dead") and DL.get("error") == dead_host_msg(DL["dead"])):
            DL["error"] = MISSING_MSG if (DL.get("error") == MISSING_MSG or missing_count(log_path) or DL["removed"]) else \
                "No images were downloaded" + (f" ({DL['error']})" if DL.get("error") else "") + ". Try another result."
        if DL["files"] == 0:
            try: os.rmdir(dest)
            except OSError: pass
        DL["running"] = False; DL["finished_at"] = time.time()
        if on_done:
            try: on_done()
            except Exception: pass
