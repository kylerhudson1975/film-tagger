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
    """Best ThePornDB match for a file, or None. Uses ?parse= with the cleaned name, then a studio/date search."""
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

def scene_fields(s):
    """Map a ThePornDB scene resource to Film Tagger columns."""
    site = s.get("site") or {}
    img = s.get("poster") or s.get("image") or ((s.get("background") or {}).get("large"))
    date = (s.get("date") or "")[:10] or None
    dur = s.get("duration")
    return dict(title=s.get("title"), studio=site.get("name"), release_date=date, year=int(date[:4]) if date else None,
                cast_=", ".join(p.get("name") for p in (s.get("performers") or []) if p.get("name")) or None,
                runtime=round(dur / 60) if isinstance(dur, (int, float)) and dur else None, plot=s.get("description") or None,
                poster=img or None, tpdb_id=str(s.get("id") or s.get("_id") or "") or None,
                tags=[t.get("name") for t in (s.get("tags") or []) if t.get("name")])
