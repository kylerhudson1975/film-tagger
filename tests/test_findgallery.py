"""Find Gallery: prefix parsing/matching, search strategy, result parsing, ranking, URL normalising, review block,
download job (subprocess mocked: nothing is downloaded). Run: python -m pytest tests/test_findgallery.py"""
import os, sys, tempfile, time
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import app, db, findgallery as fg, gallery

FORM = '''<form id="searchform"><select class="primary" id="prefixchoice" name="prefixchoice[]" multiple>
<option value="" selected>(any prefix)</option><option value="SunsetStudios_com" class="" >SunsetStudios</option>
<option value="angels_love" class="" >Angels.Love</option><option value="Other_net" class="" >Other</option></select></form>'''
def post(tid, pid, title):
    return (f'<li id="post_{pid}"><h2>Thread: <a href="threads/{tid}-slug-here" title="{title}">{title}</a></h2>'
            f'<h3 class="posttitle">X <a href="threads/{tid}-slug-here?p={pid}&amp;highlight=a#post{pid}">{title}</a></h3></li>')
RESULTS = ("<html>" + post(111, 9001, "2024.03.09 - Ann Example &amp; Bea Sample - Whatever (x50)")
           + post(222, 9002, "SunsetStudios.com - Ann Example - Something (Mar 1, 2024)")
           + post(111, 9003, "2024.03.09 - Ann Example &amp; Bea Sample - Whatever (x50)") + post(333, 9004, "Unrelated Person set") + "</html>")

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    fg._CACHE.clear(); fg._STATE["last"] = 0; fg._PREFIX.update(at=0, list=[]); fg.DL.clear(); fg.DL["running"] = False
    monkeypatch.setattr(fg, "MIN_GAP_S", 0)
    pf = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "vg_prefixes.json")
    if os.path.exists(pf): os.remove(pf)

def test_prefixes_and_match():
    pl = fg.parse_prefixes(FORM)
    assert pl == [("SunsetStudios_com", "SunsetStudios"), ("angels_love", "Angels.Love"), ("Other_net", "Other")]
    assert fg.match_prefix(["Sunset Studios"], pl) == "SunsetStudios_com"
    assert fg.match_prefix(["Angels Love", "angels.love"], pl) == "angels_love"
    assert fg.match_prefix(["Nope Studio"], pl) is None

def test_parse_results_dedup_and_post_url():
    r = fg.parse_results(RESULTS)
    assert [x["thread_id"] for x in r] == ["111", "222", "333"]
    assert r[0]["title"] == "2024.03.09 - Ann Example & Bea Sample - Whatever (x50)"
    assert r[0]["url"] == "https://vipergirls.to/threads/111" and r[0]["post_url"] == "https://vipergirls.to/threads/111?p=9001#post9001"
    th = '<a href="threads/444-x" id="thread_title_444">Thread view title</a>'
    assert fg.parse_results(th)[0]["thread_id"] == "444"

def test_rank_by_performers_studio_date_not_title():
    r = fg.rank(fg.parse_results(RESULTS), ["Ann Example", "Bea Sample"], ["Sunset Studios"], "2024-03-09")
    assert r[0]["thread_id"] == "111" and r[0]["performers_found"] == 2 and r[0]["date_boost"] == 0.5
    assert r[1]["thread_id"] == "222" and r[1]["studio_found"] and r[1]["performers_found"] == 1
    assert r[-1]["thread_id"] == "333" and r[-1]["score"] == 0

def test_normalize_url():
    n = fg.normalize_url
    assert n("https://vipergirls.to/threads/7705029-Some-Thread-(x56)") == "https://vipergirls.to/threads/7705029"
    assert n("https://vipergirls.to/threads/7604008-2017-x?s=0e1f3aa568e4dd9b25d0af5b05a50072") == "https://vipergirls.to/threads/7604008"
    assert n("vipergirls.to/threads/7705029-x?p=122855466&highlight=a#post122855466") == "https://vipergirls.to/threads/7705029?p=122855466#post122855466"
    assert n("https://vipergirls.to/threads/1-x?s=abc&amp;p=55#post55") == "https://vipergirls.to/threads/1?p=55#post55"
    assert n("https://vipergirls.to/posts/42") is None and n("https://example.com/threads/1") is None and n("") is None

# gallery-dl's own vipergirls extractor regexes (copied from gallery-dl 1.32.17; checked live against the installed package when available)
_VG_BASE = r"(?:https?://)?(?:www\.)?(?:viper(?:(?:girls)?\.to|bb\.rocks|kats\.eu|ohilia\.art|proxy\.org|vault\.link)|planetviper\.club)"
VG_PATTERNS = [_VG_BASE + r"/threads/(\d+)(?:-[^/?#]+)?(/page\d+)?(?:$|#|\?(?!p=))",
               _VG_BASE + r"/threads/(\d+)(?:-[^/?#]+)?\?p=\d+[^#]*#post(\d+)"]
try:
    from gallery_dl.extractor import vipergirls as _vg  # test only; the app never imports gallery_dl
    VG_PATTERNS = [_vg.VipergirlsThreadExtractor.pattern, _vg.VipergirlsPostExtractor.pattern]
except Exception: pass

def test_built_urls_match_gallery_dl_extractor():
    import re
    built = [x[k] for x in fg.parse_results(RESULTS) + fg.parse_results(WGP_RES) for k in ("url", "post_url") if x.get(k)]
    built += [fg.normalize_url(u) for u in ("https://vipergirls.to/threads/7604008-2017-05-22-A?s=0e1f",
              "https://vipergirls.to/threads/7604008-x?s=0e1f&amp;p=120524599#post120524599", "vipergirls.to/threads/5#post9")]
    assert "https://vipergirls.to/threads/7604008?p=120524599#post120524599" in built
    for u in built:
        assert any(re.match(p, u) for p in VG_PATTERNS), u

def scene(cl, notes=None, studio="Sunset Studios", cast="Ann Example, Bea Sample"):
    root = tempfile.mkdtemp(); p = os.path.join(root, "v.mp4"); open(p, "wb").write(b"x")
    c = db.connect()
    fid = c.execute("INSERT INTO films(title,kind,studio,cast_,release_date,notes) VALUES('T','scene',?,?,'2024-03-09',?)", (studio, cast, notes)).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, p)); c.commit()
    return fid, p

def test_search_endpoint_strategy(monkeypatch):
    calls = []
    def http(url, data=None, timeout=30):
        if data is None: return FORM
        calls.append((data["query"], data.get("prefixchoice[]")))
        return "<html></html>" if data.get("prefixchoice[]") else RESULTS
    monkeypatch.setattr(fg, "http", http)
    cl = app.app.test_client(); fid, _ = scene(cl)
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    P = ["SunsetStudios_com"]
    assert calls == [("Ann Example Bea Sample", P), ("Ann Example", P), ("Bea Sample", P), ("Ann Example Bea Sample Sunset Studios", None)]
    assert d["default_query"] == "Ann Example Bea Sample Sunset Studios" and d["prefix"] == "SunsetStudios_com"
    assert d["results"][0]["thread_id"] == "111" and len(d["tried"]) == 4
    # cached: no new HTTP call
    cl.post(f"/api/films/{fid}/findgallery/search", json={}); assert len(calls) == 4
    # custom query, prefix off
    cl.post(f"/api/films/{fid}/findgallery/search", json={"query": "Ann Example", "prefix": False})
    assert calls[-1] == ("Ann Example", None)

def test_rate_limit(monkeypatch):
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=30: FORM if data is None else RESULTS)
    monkeypatch.setattr(fg, "MIN_GAP_S", 20)
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Unknown Studio")
    assert cl.post(f"/api/films/{fid}/findgallery/search", json={"query": "a b"}).status_code == 200
    r = cl.post(f"/api/films/{fid}/findgallery/search", json={"query": "c d"})
    assert r.status_code == 429 and r.json["wait"] > 0

def test_review_blocked(monkeypatch):
    monkeypatch.setattr(fg, "http", lambda *a, **k: pytest.fail("no HTTP for review scenes"))
    cl = app.app.test_client(); fid, _ = scene(cl, notes="Review by hand")
    assert cl.post(f"/api/films/{fid}/findgallery/search", json={}).status_code == 403
    assert cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/posts/1"}).status_code == 403

class FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd = cmd; self.returncode = None; self.pid = 1; self.stderr = None
        dest = cmd[cmd.index("-D") + 1]; os.makedirs(dest, exist_ok=True)
        open(os.path.join(dest, "a.jpg"), "wb").write(b"\xff" * 20000)   # kept
        open(os.path.join(dest, "removed.png"), "wb").write(b"\x00" * 503)  # placeholder, removed
        FakeProc.last = self
    def poll(self): self.returncode = 0; return 0
    def wait(self, timeout=None): return 0
    def terminate(self): pass

def test_download_job_mocked(monkeypatch):
    cl = app.app.test_client(); fid, p = scene(cl)
    monkeypatch.setattr(fg.subprocess, "Popen", FakeProc)
    r = cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/threads/5-x?p=77#post77"}).json
    assert r["ok"] and r["url"] == "https://vipergirls.to/threads/5?p=77#post77"
    for _ in range(50):
        if not fg.DL.get("running"): break
        time.sleep(0.1)
    cmd = FakeProc.last.cmd
    assert cmd[1:3] == ["-m", "gallery_dl"] and cmd[cmd.index("-D") + 1] == gallery.canonical_dir(p) and cmd[-1] == "https://vipergirls.to/threads/5?p=77#post77"
    assert fg.DL["files"] == 1 and fg.DL["removed"] == 1 and not fg.DL["error"]
    assert cl.get(f"/api/films/{fid}").json["gallery_count"] == 1
    # existing non-empty gallery needs confirmation
    r = cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/threads/5"})
    assert r.status_code == 409 and r.json["confirm"] and r.json["existing"] == 1
    assert cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://bad.example/x"}).status_code == 400

# Kyle's first real test: unlinked scene (cast is the run-on title), guest links carry ?s=<session>.
WGP_FORM = '<select name="prefixchoice[]" multiple><option value="" selected>(any prefix)</option><option value="WhenGirlsPlay_com" class="" >WhenGirlsPlay</option></select>'
def guest_post(tid, slug, pid, title):
    return (f'<li class="imodselector postbit" id="post_{pid}"><div class="username_container"><h2>Thread: '
            f'<a href="threads/{tid}-{slug}?s=0e1f3aa568e4dd9b25d0af5b05a50072" title="{title}">{title}</a></h2></div>'
            f'<h3 class="posttitle"><a href="threads/{tid}-{slug}?s=0e1f3aa568e4dd9b25d0af5b05a50072&amp;p={pid}#post{pid}">{title}</a></h3></li>')
WGP_RES = ("<html>" + guest_post(7700001, "2018-02-26-Other-Girl-amp-Elena-Koshka-X", 122389504, "2018.02.26 - Other Girl &amp; Elena Koshka - X (x107)")
           + guest_post(7604008, "2017-05-22-Aubrey-Sinclair-amp-Elena-Koshka-Painting-Her-Pussy-(x104)", 120524599,
                        "2017.05.22 - Aubrey Sinclair &amp; Elena Koshka - Painting Her Pussy (x104)") + "</html>")

def test_guest_session_links_parse():
    r = fg.parse_results(WGP_RES)
    assert [x["thread_id"] for x in r] == ["7700001", "7604008"]
    assert r[1]["post_url"] == "https://vipergirls.to/threads/7604008?p=120524599#post120524599"

def test_guess_names():
    assert fg.guess_names("Elena Koshka Aubrey Sinclair", ["When Girls Play"]) == ["Elena Koshka", "Aubrey Sinclair"]
    assert fg.guess_names("WhenGirlsPlay Elena Koshka Aubrey Sinclair", ["When Girls Play"]) == ["Elena Koshka", "Aubrey Sinclair"]

def test_unlinked_scene_finds_thread(monkeypatch):
    calls = []
    def http(url, data=None, timeout=30):
        if data is None: return WGP_FORM
        calls.append((data["query"], data.get("prefixchoice[]")))
        return WGP_RES if data["query"] == "Elena Koshka" else "<html>no matches</html>"
    monkeypatch.setattr(fg, "http", http)
    cl = app.app.test_client(); fid, _ = scene(cl, studio="When Girls Play", cast="Elena Koshka Aubrey Sinclair")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert d["prefix"] == "WhenGirlsPlay_com"
    assert calls[:2] == [("Elena Koshka Aubrey Sinclair", ["WhenGirlsPlay_com"]), ("Elena Koshka", ["WhenGirlsPlay_com"])]
    assert d["results"][0]["thread_id"] == "7604008" and d["results"][0]["performers_found"] == 2
