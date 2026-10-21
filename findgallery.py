"""Find Gallery: search ViperGirls (as a guest, no login) for a scene's photo set and download it with gallery-dl.

gallery-dl (GPL-2.0, https://github.com/mikf/gallery-dl) is used only as a separate program via subprocess
(`python -m gallery_dl`); it is never imported. Search strategy (thread titles are unreliable, so the scene title is
never used): performer keyword(s) limited to the studio's prefix tag, else performers + studio as plain text.
Results are ranked by how many of the scene's performers and its studio appear in the thread title, with a small
boost when a date in the title is close to the release date."""
import os, re, sys, json, time, html, threading, subprocess, urllib.request, urllib.parse, datetime
import db

BASE = "https://vipergirls.to/"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FilmTagger"
MIN_GAP_S = 20          # at least 20 s between searches (be polite to the forum)
CACHE_S = 3600          # cache results for an hour
PREFIX_TTL_S = 7 * 86400
MIN_FILE_BYTES = 10 * 1024
PLACEHOLDER_SIZES = {8090, 9584, 503, 2142}  # known 'image removed' / placeholder sizes (anything < 10 KB is dropped anyway)
DL_TIMEOUT_S = 45 * 60

_LOCK = threading.Lock(); _STATE = {"last": 0.0}; _CACHE = {}; _PREFIX = {"at": 0, "list": []}

def http(url, data=None, timeout=30):
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

def prefixes(cache_file=None):
    if _PREFIX["list"] and time.time() - _PREFIX["at"] < PREFIX_TTL_S: return _PREFIX["list"]
    if cache_file and os.path.exists(cache_file):
        try:
            d = json.load(open(cache_file, encoding="utf-8"))
            if time.time() - d["at"] < PREFIX_TTL_S: _PREFIX.update(at=d["at"], list=[tuple(x) for x in d["list"]]); return _PREFIX["list"]
        except Exception: pass
    lst = parse_prefixes(http(BASE + "search.php?search_type=1"))
    if lst:
        _PREFIX.update(at=time.time(), list=lst)
        if cache_file:
            try: json.dump({"at": _PREFIX["at"], "list": lst}, open(cache_file, "w", encoding="utf-8"))
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

def form(query, prefix=None):
    d = {"do": "process", "securitytoken": "guest", "searchfromtype": "vBForum:Post", "contenttypeid": "1", "query": query,
         "titleonly": "0", "showposts": "1", "childforums": "1", "forumchoice[]": [""], "sortby": "dateline", "order": "descending",
         "searchdate": "0", "beforeafter": "after", "dosearch": "Search Now"}
    if prefix: d["prefixchoice[]"] = [prefix]
    return d

def wait_s():
    return max(0.0, MIN_GAP_S - (time.time() - _STATE["last"]))

def search(query, prefix=None, block=False):
    """One forum search (cached). Raises Busy(seconds) if another search ran less than 20 s ago (unless block)."""
    ck = (query.strip().lower(), prefix or "")
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < CACHE_S: return hit[1], True
    with _LOCK:  # one search at a time
        w = wait_s()
        if w > 0:
            if not block: raise Busy(w)
            time.sleep(w)
        try: page = http(BASE + "search.php?do=process", form(query, prefix))
        finally: _STATE["last"] = time.time()
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
    pk = [db.fold(" ".join(p.split())) for p in performers if p.strip()]
    sk = {key(s) for s in studio_names if s} - {""}
    for r in results:
        t = db.fold(r["title"]); tk = key(r["title"])
        found = [p for p in pk if p in t or key(p) in tk]
        st = bool(prefix_used) or any(k and k in tk for k in sk)
        boost = 0.0
        if rd:
            ds = [abs((d - rd).days) for d in title_dates(r["title"])]
            if ds: boost = 0.5 if min(ds) == 0 else 0.3 if min(ds) <= 7 else 0.1 if min(ds) <= 31 else 0.0
        r.update(score=round(len(found) + (1 if st else 0) + boost, 2), performers_found=len(found), studio_found=st, date_boost=boost)
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

# ---------- download (gallery-dl as a separate process) ----------
DL = {"running": False}

def gallery_dl_cmd(url, dest):
    return [sys.executable, "-m", "gallery_dl", "--no-mtime", "-D", dest, url]

def count_files(d):
    try: return sum(1 for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)) and not n.endswith((".part", ".tmp")))
    except OSError: return 0

def cleanup(d):
    """Drop placeholders / tiny files (< 10 KB or a known 'removed' size) and leftovers. Returns files removed."""
    n = 0
    try: names = os.listdir(d)
    except OSError: return 0
    for x in names:
        p = os.path.join(d, x)
        if not os.path.isfile(p): continue
        sz = os.path.getsize(p)
        if sz < MIN_FILE_BYTES or sz in PLACEHOLDER_SIZES or x.endswith((".part", ".tmp")):
            try: os.remove(p); n += 1
            except OSError: pass
    return n

def run_download(url, dest, on_done=None, popen=None, timeout=DL_TIMEOUT_S):
    popen = popen or subprocess.Popen
    os.makedirs(dest, exist_ok=True)
    DL.update(running=True, url=url, dest=dest, files=count_files(dest), error=None, cancel=False, started_at=time.time(), removed=0, rc=None)
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = popen(gallery_dl_cmd(url, dest), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=flags)
        DL["pid"] = getattr(p, "pid", None)
        while p.poll() is None:
            DL["files"] = count_files(dest)
            if DL["cancel"]: p.terminate(); DL["error"] = "cancelled"; break
            if time.time() - DL["started_at"] > timeout: p.terminate(); DL["error"] = "timed out"; break
            time.sleep(1)
        try: p.wait(timeout=20)
        except Exception:
            try: p.kill()
            except Exception: pass
        DL["rc"] = p.returncode
        if p.returncode not in (0, None) and not DL["error"]:
            try: err = (p.stderr.read() or b"").decode("utf-8", "replace") if p.stderr else ""
            except Exception: err = ""
            DL["error"] = (err.strip().splitlines() or [f"gallery-dl exit code {p.returncode}"])[-1][:300]
    except Exception as e:
        DL["error"] = f"{type(e).__name__}: {e}"
    finally:
        DL["removed"] = cleanup(dest); DL["files"] = count_files(dest)
        if DL["files"] == 0:
            try: os.rmdir(dest)
            except OSError: pass
        DL["running"] = False; DL["finished_at"] = time.time()
        if on_done:
            try: on_done()
            except Exception: pass
