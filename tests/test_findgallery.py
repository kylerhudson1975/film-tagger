"""Find Gallery: prefix parsing/matching, search strategy, result parsing, ranking, URL normalising, review block,
download job (subprocess mocked: nothing is downloaded). Run: python -m pytest tests/test_findgallery.py"""
import os, sys, tempfile, time, json
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
    fg._CACHE.clear(); fg._STATE["last"] = 0; fg._PREFIX.update(at=0, list=[], forum=None); fg.DL.clear(); fg.DL["running"] = False
    monkeypatch.setattr(fg, "MIN_GAP_S", 0)
    import tpdb; monkeypatch.setattr(tpdb, "call", lambda *a, **k: {"data": []})  # never reach the real ThePornDB
    app.FG_KNOWN.clear(); app.FG_ALIAS.clear()
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

def scene(cl, notes=None, studio="Sunset Studios", cast="Ann Example, Bea Sample", tid="m1"):
    root = tempfile.mkdtemp(); p = os.path.join(root, "v.mp4"); open(p, "wb").write(b"x")
    c = db.connect()
    fid = c.execute("INSERT INTO films(title,kind,studio,cast_,release_date,notes,tpdb_id) VALUES('T','scene',?,?,'2024-03-09',?,?)", (studio, cast, notes, tid)).lastrowid
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
    assert calls == [("Ann Example", P), ("Bea Sample", P), ("Ann Example Bea Sample Sunset Studios", None)]
    assert d["default_query"] == "Ann Example Bea Sample Sunset Studios" and d["prefix"] == "SunsetStudios_com"
    assert d["results"][0]["thread_id"] == "111" and len(d["tried"]) == 3
    # cached: no new HTTP call
    cl.post(f"/api/films/{fid}/findgallery/search", json={}); assert len(calls) == 3
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
    c = db.connect(); c.executescript(app.peoplemod.PEOPLE_SCHEMA); app.peoplemod.migrate(c)
    for n in ("Elena Koshka", "Aubrey Sinclair"):
        c.execute("INSERT OR IGNORE INTO people(name, name_norm) VALUES(?,?)", (n, db.fold(n)))
    c.commit()
    cl = app.app.test_client(); fid, _ = scene(cl, studio="When Girls Play", cast="Elena Koshka Aubrey Sinclair", tid=None)
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert d["prefix"] == "WhenGirlsPlay_com"
    assert calls == [("Elena Koshka", ["WhenGirlsPlay_com"])]
    assert d["results"][0]["thread_id"] == "7604008" and d["results"][0]["performers_found"] == 2

# Hang fix: timeouts, lock always released, capped steps, time budget, progress status.
def test_http_timeout_passed_and_lock_released_on_error(monkeypatch):
    seen = []
    def http(url, data=None, timeout=None):
        seen.append(timeout); raise TimeoutError("timed out")
    monkeypatch.setattr(fg, "http", http)
    with pytest.raises(TimeoutError): fg.search("a b", None)
    assert seen == [fg.HTTP_TIMEOUT_S] and fg.HTTP_TIMEOUT_S <= 20
    assert not fg._LOCK.locked()

def test_endpoint_timeout_gives_502_and_releases(monkeypatch):
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else (_ for _ in ()).throw(TimeoutError("timed out")))
    cl = app.app.test_client(); fid, _ = scene(cl)
    r = cl.post(f"/api/films/{fid}/findgallery/search", json={})
    assert r.status_code == 502 and "did not answer" in r.json["error"]
    assert not fg._LOCK.locked() and cl.get("/api/findgallery/status").json["search"] is None

def test_held_lock_gives_429_not_hang(monkeypatch):
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else pytest.fail("no search while locked"))
    fg._LOCK.acquire()
    try:
        t = time.time(); cl = app.app.test_client(); fid, _ = scene(cl)
        r = cl.post(f"/api/films/{fid}/findgallery/search", json={"query": "x y"})
        assert r.status_code == 429 and time.time() - t < 5
    finally: fg._LOCK.release()

def test_steps_capped(monkeypatch):
    calls = []
    def http(url, data=None, timeout=None):
        if data is None: return FORM
        calls.append(data["query"]); return "<html></html>"
    monkeypatch.setattr(fg, "http", http)
    cl = app.app.test_client(); fid, _ = scene(cl, cast="Ann Example, Bea Sample, Cat Third, Dee Fourth")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert len(calls) <= fg.MAX_STEPS == 7 and d["results"] == []

def test_time_budget_stops_new_steps(monkeypatch):
    calls = []
    def http(url, data=None, timeout=None):
        if data is None: return FORM
        calls.append(data["query"]); return "<html></html>"
    monkeypatch.setattr(fg, "http", http); monkeypatch.setattr(fg, "SEARCH_BUDGET_S", -1)
    cl = app.app.test_client(); fid, _ = scene(cl)
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert len(calls) == 1 and d["tried"][-1].get("skipped") == "time limit"

def test_single_performer_charity_case(monkeypatch):
    form = '<select name="prefixchoice[]"><option value="Cum4K_com" class="" >Cum4K</option></select>'
    res = "<html>" + guest_post(4228056, "Charity-Crawford-Creampies-For-Charity-50x-1500x1000-01-15-2019", 41239419,
                                "Charity Crawford - Creampies For Charity  50x 1500x1000 01-15-2019") + "</html>"
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: form if data is None else res)
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Cum 4K", cast="Charity Crawford")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert d["prefix"] == "Cum4K_com" and d["results"][0]["thread_id"] == "4228056"

# Stall fix: gallery-dl writing lots of warnings must not block (a full, unread stderr pipe froze it at 0 files).
def test_noisy_subprocess_does_not_stall(monkeypatch, tmp_path):
    script = ("import sys,os\nd=sys.argv[1]\nfor i in range(5000): sys.stderr.write('[vipr][warning] noisy line %d\\n' % i)\n"
              "os.makedirs(d, exist_ok=True); open(os.path.join(d,'a.jpg'),'wb').write(b'x'*20000)\nsys.stderr.write('done\\n')\n")
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: [sys.executable, "-c", script, dest])
    dest = str(tmp_path / "v - GALLERY"); t = time.time()
    fg.run_download("https://vipergirls.to/threads/1", dest, timeout=30)
    assert time.time() - t < 20 and not fg.DL["error"] and fg.DL["files"] == 1 and fg.DL["rc"] == 0
    assert fg.log_tail(fg.DL["log"]) == "done"

def test_dead_set_stops_early_with_plain_message(monkeypatch, tmp_path):
    script = ("import sys,time\nfor i in range(200):\n sys.stderr.write('[vipr][error] NotFoundError: Requested image could not be found\\n'); sys.stderr.flush(); time.sleep(0.01)\n")
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: [sys.executable, "-c", script, dest])
    dest = tmp_path / "v - GALLERY"
    fg.run_download("https://vipergirls.to/threads/1", str(dest), timeout=30)
    assert fg.DL["files"] == 0 and fg.DL["error"] == fg.MISSING_MSG and "missing" in fg.DL["error"]
    assert not dest.exists()  # empty folder removed

def test_zero_files_other_error_message(monkeypatch, tmp_path):
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: [sys.executable, "-c", "import sys; sys.stderr.write('[x][error] boom\\n'); sys.exit(1)"])
    fg.run_download("https://vipergirls.to/threads/1", str(tmp_path / "w - GALLERY"), timeout=30)
    assert fg.DL["error"].startswith("No images were downloaded") and "boom" in fg.DL["error"]

def test_placeholders_removed_and_dead_set_message(monkeypatch, tmp_path):
    # 12 Imagetwist 'deleted' placeholders (8183 bytes) and 4 identical 30 KB placeholders -> nothing real kept
    script = ("import sys,os,time\nd=sys.argv[1]; os.makedirs(d, exist_ok=True)\n"
              "for i in range(12): open(os.path.join(d,'it%d.jpg'%i),'wb').write(b'\\x01'*8183)\n"
              "for i in range(4): open(os.path.join(d,'dup%d.jpg'%i),'wb').write(b'\\x02'*30000)\n")
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: [sys.executable, "-c", script, dest])
    dest = tmp_path / "p - GALLERY"
    fg.run_download("https://vipergirls.to/threads/1", str(dest), timeout=30)
    assert fg.DL["files"] == 0 and fg.DL["removed"] == 16 and fg.DL["error"] == fg.MISSING_MSG and "deleted" in fg.DL["error"]
    assert not dest.exists()

def test_real_photos_kept_next_to_placeholders(tmp_path):
    d = tmp_path / "g"; d.mkdir()
    for i in range(3): (d / f"real{i}.jpg").write_bytes(bytes([i]) * 40000)  # same size, different content: kept
    (d / "ph.jpg").write_bytes(b"\x00" * 8183)
    assert fg.cleanup(str(d)) == 1 and sorted(x.name for x in d.iterdir()) == ["real0.jpg", "real1.jpg", "real2.jpg"]


# Search only in 'Adult Photo Sets' (+ subforums), id read from the search form
FORUM_FORM = (FORM.replace("</form>", "") + '<select name="forumchoice[]" multiple><option value="" selected>Search All Open Forums</option>'
              '<option value="235" class="d0" > Adult Photo Sets</option><option value="304" class="d1" > Hardcore Photo Sets</option></select></form>')

def test_parse_forum_id():
    assert fg.parse_forum_id(FORUM_FORM) == "235" and fg.parse_forum_id("<html></html>") is None

def test_every_step_restricted_to_forum(monkeypatch):
    sent = []
    def http(url, data=None, timeout=None):
        if data is None: return FORUM_FORM.replace('value="235"', 'value="999"')
        sent.append((data["forumchoice[]"], data["childforums"])); return "<html></html>"
    monkeypatch.setattr(fg, "http", http)
    cl = app.app.test_client(); fid, _ = scene(cl)
    cl.post(f"/api/films/{fid}/findgallery/search", json={})
    assert len(sent) == 4 and all(x == (["999"], "1") for x in sent)  # every step (incl. last name) stays in the forum

def test_forum_fallback_with_warning(monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else sent.append(data["forumchoice[]"]) or "<html></html>")
    cl = app.app.test_client(); fid, _ = scene(cl)
    with caplog.at_level("WARNING"): cl.post(f"/api/films/{fid}/findgallery/search", json={"query": "a b"})
    assert sent == [[fg.FORUM_FALLBACK]] and "Adult Photo Sets" in caplog.text


# 'Cum Fetish' (Octavia Red, Brad Sterling): the thread title leaves out the male co-star, so ANDing both names found nothing.
def test_first_performer_alone_first_and_wide_last(monkeypatch):
    form = '<select name="prefixchoice[]"><option value="Cum4K_com" class="" >Cum4K</option></select>'
    res = "<html>" + guest_post(7578943, "Octavia-Red-Cum-Fetish-06-Dec", 1, "Octavia Red - Cum Fetish 06 Dec") + "</html>"
    sent = []
    def http(url, data=None, timeout=None):
        if data is None: return form
        sent.append((data["query"], data.get("prefixchoice[]"), data["forumchoice[]"]))
        return res if data["query"] == "Octavia Red" else "<html></html>"
    monkeypatch.setattr(fg, "http", http)
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Cum 4K", cast="Octavia Red, Brad Sterling")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert sent == [("Octavia Red", ["Cum4K_com"], [fg.FORUM_FALLBACK])] and d["results"][0]["thread_id"] == "7578943"

def test_no_all_forums_step_and_cache_key_has_forum(monkeypatch):
    sent = []
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else sent.append(data["forumchoice[]"]) or "<html></html>")
    cl = app.app.test_client(); fid, _ = scene(cl, cast="Ann Example")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert sent and all(x == [fg.FORUM_FALLBACK] for x in sent) and not any(t.get("all_forums") for t in d["tried"])
    fg._PREFIX["forum"] = "777"; n = len(sent)
    fg.search("Ann Example", "SunsetStudios_com")
    assert len(sent) == n + 1  # different forum -> not served from the old cache

# Thread 10331171: the search hit was a reply post with no images; the set is in post 1, so retry with the whole thread.
def test_empty_post_retries_whole_thread(monkeypatch, tmp_path):
    script = ("import sys,os\nd,u=sys.argv[1],sys.argv[2]\nos.makedirs(d, exist_ok=True)\n"
              "if '?p=' not in u:\n for i in range(3): open(os.path.join(d,'%d.jpg'%i),'wb').write(bytes([i])*20000)\n")
    urls = []
    def cmd(url, dest): urls.append(url); return [sys.executable, "-c", script, dest, url]
    monkeypatch.setattr(fg, "gallery_dl_cmd", cmd)
    fg.run_download("https://vipergirls.to/threads/10331171?p=170849740#post170849740", str(tmp_path / "g"), timeout=30)
    assert urls == ["https://vipergirls.to/threads/10331171?p=170849740#post170849740", "https://vipergirls.to/threads/10331171"]
    assert fg.DL["files"] == 3 and not fg.DL["error"]

def test_thread_url_not_retried(monkeypatch, tmp_path):
    urls = []
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: urls.append(url) or [sys.executable, "-c", "pass"])
    fg.run_download("https://vipergirls.to/threads/5", str(tmp_path / "h"), timeout=30)
    assert urls == ["https://vipergirls.to/threads/5"] and fg.DL["error"].startswith("No images")

# Aliases: 'Semen Desires' (Tori Mack) is posted under her alias 'Tiana Blow'.
SD_FORM = '<select name="prefixchoice[]"><option value="Cum4K_com" class="" >Cum4K</option></select>'
SD_RES = "<html>" + guest_post(7830780, "Semen-Desires-Tiana-Blow-14-Feb", 1, "Semen Desires - Tiana Blow 14 Feb") + "</html>"

def _sd_http(sent):
    def http(url, data=None, timeout=None):
        if data is None: return SD_FORM
        sent.append((data["query"], data.get("prefixchoice[]"), data["forumchoice[]"]))
        return SD_RES if data["query"] == "Tiana Blow" else "<html></html>"
    return http

def test_alias_from_tpdb_cached_add_only(monkeypatch):
    sent = []; app.FG_ALIAS.clear()
    monkeypatch.setattr(fg, "http", _sd_http(sent))
    import tpdb
    monkeypatch.setattr(tpdb, "call", lambda path, token, **q: {"data": [{"id": "p1", "name": "Tori Mack", "aliases": ["Tiana Blow", "Tori"]}]})
    c = db.connect(); db.set_setting(c, "tpdb_key", "dummy"); c.executescript(app.peoplemod.PEOPLE_SCHEMA); app.peoplemod.migrate(c)
    c.execute("INSERT INTO people(name, name_norm, aliases) VALUES('Tori Mack', ?, ?)", (db.fold("Tori Mack"), '["Kyle Set Alias X"]')); c.commit()
    db._AG["key"] = None
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Cum 4K", cast="Tori Mack, Quinton James")
    # DB alias 'Kyle Set Alias X' exists, so the DB is used first; drop it to exercise ThePornDB
    c.execute("UPDATE people SET aliases='[\"Tori\"]' WHERE name_norm=?", (db.fold("Tori Mack"),)); c.commit(); db._AG["key"] = None
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    P = ["Cum4K_com"]; F = [fg.FORUM_FALLBACK]
    assert sent == [("Tori Mack", P, F), ("Quinton James", P, F), ("Tiana Blow", P, F)]
    assert d["results"][0]["thread_id"] == "7830780" and d["results"][0]["matched"] == ["Tiana Blow"]
    assert d["aliases"]["Tori Mack"] == {"names": ["Tiana Blow"], "source": "tpdb"}
    al = json.loads(c.execute("SELECT aliases FROM people WHERE name_norm=?", (db.fold("Tori Mack"),)).fetchone()[0])
    assert al == ["Tori", "Tiana Blow"]  # add-only: the existing entry stays

def test_alias_from_db_no_tpdb(monkeypatch):
    sent = []; app.FG_ALIAS.clear()
    monkeypatch.setattr(fg, "http", _sd_http(sent))
    import tpdb
    monkeypatch.setattr(tpdb, "call", lambda *a, **k: pytest.fail("DB alias known: no ThePornDB call"))
    c = db.connect(); c.executescript(app.peoplemod.PEOPLE_SCHEMA); app.peoplemod.migrate(c)
    c.execute("INSERT INTO people(name, name_norm, aliases) VALUES('Tori Mackk', ?, ?)", (db.fold("Tori Mackk"), '["Tiana Blow"]')); c.commit(); db._AG["key"] = None
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Cum 4K", cast="Tori Mackk")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert d["aliases"]["Tori Mackk"]["source"] == "db" and d["results"][0]["thread_id"] == "7830780"

def test_rank_alias_counts_as_performer():
    r = fg.rank([dict(title="Semen Desires - Tiana Blow 14 Feb")], [["Tori Mack", "Tiana Blow"], ["Quinton James"]], ["Cum 4K"])
    assert r[0]["performers_found"] == 1 and r[0]["matched"] == ["Tiana Blow"]

# Last-name fallback: ThePornDB only knows 'Tianna Blow'/'Tatiana Blow'; the thread says 'Tiana Blow'. 'Blow' + Cum4K finds it.
def test_last_name_fallback(monkeypatch):
    sent = []; app.FG_ALIAS.clear()
    def http(url, data=None, timeout=None):
        if data is None: return SD_FORM
        sent.append((data["query"], data.get("prefixchoice[]"), data["forumchoice[]"]))
        return SD_RES if data["query"] == "Blow" else "<html></html>"
    monkeypatch.setattr(fg, "http", http)
    import tpdb
    monkeypatch.setattr(tpdb, "call", lambda path, token, **q: {"data": [{"id": "p", "name": "Tori Macke", "aliases": ["Tianna Blow", "Tatiana Blow"]}]})
    c = db.connect(); db.set_setting(c, "tpdb_key", "dummy"); c.executescript(app.peoplemod.PEOPLE_SCHEMA); app.peoplemod.migrate(c)
    c.execute("INSERT INTO people(name, name_norm, aliases) VALUES('Quinton Jamez', ?, ?)", (db.fold("Quinton Jamez"), '["James Quintonz", "Quintin Jamez"]')); c.commit(); db._AG["key"] = None
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Cum 4K", cast="Tori Macke, Quinton Jamez")
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    P = ["Cum4K_com"]
    assert [x[0] for x in sent] == ["Tori Macke", "Quinton Jamez", "Tianna Blow", "Tatiana Blow", "Tori Macke Quinton Jamez Cum 4K", "Macke", "Blow"]
    assert len(sent) <= fg.MAX_STEPS == 7 and all(x[2] == [fg.FORUM_FALLBACK] for x in sent)  # co-star aliases trimmed, forum kept
    assert all(x[1] == P for x in sent if x[0] in ("Macke", "Blow"))
    r = d["results"][0]
    assert r["thread_id"] == "7830780" and r["last_name_match"] and d["tried"][-1]["last_name"]

def test_no_last_name_step_without_prefix(monkeypatch):
    sent = []
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else sent.append(data["query"]) or "<html></html>")
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Unknown Studio", cast="Al Bo")
    cl.post(f"/api/films/{fid}/findgallery/search", json={})
    assert sent == ["Al Bo Unknown Studio"]  # no prefix -> no last-name step ('Bo' would be too short anyway)

# Titles never become keywords: run-on casts keep only known performer pairs; unknown -> empty query with a hint.
def _no_title_words(queries, title):
    tw = {w.lower() for w in title.split() if len(w) > 2}
    return all(not (set(q.lower().split()) & tw) for q in queries)

def test_runon_cast_keeps_only_known_pairs(monkeypatch):
    sent = []; app.FG_KNOWN.clear(); app.FG_ALIAS.clear()
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else sent.append(data["query"]) or "<html></html>")
    import tpdb
    monkeypatch.setattr(tpdb, "call", lambda path, token, **q: {"data": [{"id": 1, "name": "Chloe Rosey", "aliases": []}]} if q.get("q") == "Chloe Rosey" else {"data": []})
    c = db.connect(); db.set_setting(c, "tpdb_key", "dummy"); c.commit()
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Sunset Studios", cast="SunsetStudios Chloe Rosey Come Fetch Me", tid=None)
    c.execute("UPDATE films SET title='SunsetStudios Chloe Rosey Come Fetch Me' WHERE id=?", (fid,)); c.commit()
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert d["default_query"] == "Chloe Rosey Sunset Studios"
    assert sent and _no_title_words(sent, "Come Fetch Me")

def test_unknown_names_give_empty_query_and_hint(monkeypatch):
    app.FG_KNOWN.clear()
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else pytest.fail("no search without names"))
    import tpdb
    monkeypatch.setattr(tpdb, "call", lambda *a, **k: {"data": []})
    c = db.connect(); db.set_setting(c, "tpdb_key", "dummy"); c.commit()
    cl = app.app.test_client(); fid, _ = scene(cl, studio="Sunset Studios", cast="Some Random Title Words Here", tid=None)
    r = cl.post(f"/api/films/{fid}/findgallery/search", json={})
    assert r.status_code == 400 and r.json["default_query"] == "" and "add performers or type a name" in r.json["error"]

def test_matched_scene_trusts_cast_and_no_title(monkeypatch):
    sent = []
    monkeypatch.setattr(fg, "http", lambda url, data=None, timeout=None: FORM if data is None else sent.append(data["query"]) or "<html></html>")
    cl = app.app.test_client(); fid, _ = scene(cl, cast="Ann Example, Bea Sample")
    c = db.connect(); c.execute("UPDATE films SET tpdb_id='x1', title='Painting Party Time' WHERE id=?", (fid,)); c.commit()
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    assert d["default_query"] == "Ann Example Bea Sample Sunset Studios" and _no_title_words(sent, "Painting Party Time")

def test_rank_has_no_title_word_boost():
    a = fg.rank([dict(title="Ann Example - Painting Party Time")], ["Ann Example"], [], None)[0]
    b = fg.rank([dict(title="Ann Example - something else")], ["Ann Example"], [], None)[0]
    assert a["score"] == b["score"]

# Thread 2358460: posts embed promo images from images.passionhdfan.com (NXDOMAIN) before the real imx.to set;
# gallery-dl retried every dead image and saved nothing. Stop on repeated DNS failures and skip that host.
DNS_LINE = "[downloader.http][warning] NameResolutionError: Failed to resolve 'images.deadfan.com' ([Errno 11001] getaddrinfo failed)\\n"

def test_dead_host_skipped_and_rest_downloaded(monkeypatch, tmp_path):
    hang = "import sys,time\nfor i in range(500): sys.stderr.write(%r); sys.stderr.flush(); time.sleep(0.05)\n" % DNS_LINE.replace("\\n", "\n")
    ok = "import sys,os\nd=sys.argv[1]; os.makedirs(d, exist_ok=True)\nfor i in range(3): open(os.path.join(d,'%d.jpg'%i),'wb').write(bytes([i+1])*20000)\n"
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: [sys.executable, "-c", hang])
    files = {}
    def ifc(path, dest): files["urls"] = open(path).read().split(); return [sys.executable, "-c", ok, dest]
    monkeypatch.setattr(fg, "input_file_cmd", ifc)
    monkeypatch.setattr(fg, "list_urls", lambda u: ["http://images.deadfan.com/a.jpg", "https://imx.to/img-1.html", "https://imx.to/img-2.html"])
    monkeypatch.setattr(fg, "resolves", lambda h: h != "images.deadfan.com")
    t = time.time(); fg.run_download("https://vipergirls.to/threads/2358460", str(tmp_path / "g"), timeout=60)
    assert time.time() - t < 15 and files["urls"] == ["https://imx.to/img-1.html", "https://imx.to/img-2.html"]
    assert fg.DL["files"] == 3 and not fg.DL["error"]

def test_only_dead_host_clear_message(monkeypatch, tmp_path):
    hang = "import sys,time\nfor i in range(500): sys.stderr.write(%r); sys.stderr.flush(); time.sleep(0.05)\n" % DNS_LINE.replace("\\n", "\n")
    monkeypatch.setattr(fg, "gallery_dl_cmd", lambda url, dest: [sys.executable, "-c", hang])
    monkeypatch.setattr(fg, "list_urls", lambda u: ["http://images.deadfan.com/a.jpg"])
    fg.run_download("https://vipergirls.to/threads/9", str(tmp_path / "g"), timeout=60)
    assert fg.DL["files"] == 0 and "no longer exists (images.deadfan.com)" in fg.DL["error"]

def test_live_urls_filters_unresolvable():
    keep, dead = fg.live_urls(["http://x.dead/1.jpg", "https://imx.to/i.html"], ok=lambda h: h == "imx.to")
    assert keep == ["https://imx.to/i.html"] and dead == {"x.dead"}
