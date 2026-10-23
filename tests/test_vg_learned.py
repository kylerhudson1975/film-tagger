"""Find Gallery learned ViperGirls rules: learned only from hand-pasted thread URLs (mocked forum, nothing downloaded)."""
import os, sys, tempfile
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
import app, db, findgallery as fg

FORM = '''<select name="prefixchoice[]" multiple><option value="">(any prefix)</option><option value="AdultTime">AdultTime</option>
<option value="UndercoverAffairs_com">UndercoverAffairs</option><option value="BadMILFS">BadMILFS</option></select>
<select name="forumchoice[]"><option value="235">Adult Photo Sets</option></select>'''
def thread(title, slug, forum="Adult Photo Sets"):
    return (f'<li class="navbit"><a href="forums/235-Adult-Photo-Sets?s=x">{forum}</a></li><h1>Thread: <span class="threadtitle">'
            f'<a href="threads/9100-{slug}?s=abc" title="Reload this Page">{title}</a></span></h1>')
NO_NAMES = thread("AdultTime Undercover Affairs - The Secret Meeting 60x", "Undercover-Affairs-The-Secret-Meeting-60x")
WITH_NAMES = thread("BadMILFS JC Wilds, Artemisia Love - Keeping Her Happy 75x", "JC-Wilds-Artemisia-Love-Keeping-Her-Happy-75x")

@pytest.fixture(autouse=True)
def reset(monkeypatch):
    fg._CACHE.clear(); fg._STATE["last"] = 0; fg._PREFIX.update(at=0, list=[], forum=None); fg.DL.clear(); fg.DL["running"] = False
    monkeypatch.setattr(fg, "MIN_GAP_S", 0)
    monkeypatch.setattr(fg, "run_download", lambda *a, **k: None)  # never download
    import tpdb; monkeypatch.setattr(tpdb, "call", lambda *a, **k: {"data": []})
    app.FG_KNOWN.clear(); app.FG_ALIAS.clear()
    c = db.connect(); c.execute(db.VG_SCHEMA); c.execute("DELETE FROM vg_learned"); c.commit()
    pf = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "vg_prefixes.json")
    if os.path.exists(pf): os.remove(pf)

def scene(studio="Undercover Affairs", cast="Ann Example, Bea Sample", notes=None):
    root = tempfile.mkdtemp(); p = os.path.join(root, "v.mp4"); open(p, "wb").write(b"x")
    c = db.connect()
    fid = c.execute("INSERT INTO films(title,kind,studio,cast_,release_date,notes,tpdb_id) VALUES('The Secret Meeting','scene',?,?,'2024-03-09',?,'m1')", (studio, cast, notes)).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, p)); c.commit(); return fid

def fake_http(pages, calls):
    def http(url, data=None, timeout=30):
        if data is not None: calls.append((data["query"], (data.get("prefixchoice[]") or [None])[0], data["forumchoice[]"])); return "<html></html>"
        if "search.php" in url: return FORM
        pages.append(url); return NO_NAMES if "9100" in url else WITH_NAMES
    return http

PL = fg.parse_prefixes(FORM)

def test_title_prefix_and_names():
    i = fg.thread_info(WITH_NAMES)
    assert i["title"].startswith("BadMILFS") and i["forums"] == ["Adult Photo Sets"]
    assert fg.title_prefix(i["title"], i["slug"], PL) == ("BadMILFS", "BadMILFS")
    assert fg.names_in_title(i["title"], [["JC Wilds"], ["Artemisia Love"]]) is True
    r = fg.learn(NO_NAMES, "Undercover Affairs", [["Ann Example", "Annie X"]], PL)
    assert r["prefix"] == "AdultTime" and r["names_in_title"] is False and "leave performer names out" in r["notes"]
    assert fg.learn(NO_NAMES, "Undercover Affairs", [], PL)["names_in_title"] is None
    assert fg.learn(thread("AdultTime X", "X", forum="Other Forum"), "S", [["A B"]], PL) is None  # outside Adult Photo Sets

def test_manual_paste_learns_and_search_uses_it(monkeypatch):
    pages, calls = [], []; monkeypatch.setattr(fg, "http", fake_http(pages, calls))
    cl = app.app.test_client(); fid = scene()
    # automatic search before learning: old prefix
    cl.post(f"/api/films/{fid}/findgallery/search", json={})
    assert calls[0][:2] == ("Ann Example", "UndercoverAffairs_com")
    assert db.vg_learned_list(db.connect()) == []  # automatic results never teach
    # automatic-result download (no manual flag) does not learn
    cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/threads/9100-x"})
    assert db.vg_learned_list(db.connect()) == [] and pages == []
    fg.DL["running"] = False  # the mocked job never finishes on its own
    # hand-pasted URL learns
    d = cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/threads/9100-x?p=5#post5", "manual": True}).json
    assert d["ok"] and d["learned"]["prefix"] == "AdultTime" and pages == ["https://vipergirls.to/threads/9100"]
    rules = db.vg_learned_list(db.connect()); assert len(rules) == 1
    assert rules[0]["studio_key"] == db.studio_key("undercover-affairs!") and rules[0]["names_in_title"] == 0
    assert rules[0]["source_url"] == "https://vipergirls.to/threads/9100"
    # next search: learned prefix first, last names early, old prefix later, never the title, forum always 235
    fg._CACHE.clear(); calls.clear()
    d = cl.post(f"/api/films/{fid}/findgallery/search", json={}).json
    qs = [(q, p) for q, p, _ in calls]
    assert qs[0] == ("Ann Example", "AdultTime") and ("Example", "AdultTime") in qs and ("Ann Example", "UndercoverAffairs_com") in qs
    assert qs.index(("Example", "AdultTime")) < qs.index(("Ann Example", "UndercoverAffairs_com"))
    assert all("secret" not in q.lower() for q, _ in qs) and all(f == ["235"] for *_, f in calls) and len(calls) <= fg.MAX_STEPS
    assert d["learned"]["prefix"] == "AdultTime" and "Learned" not in d["learned"]["notes"]

def test_new_paste_wins_and_delete(monkeypatch):
    pages, calls = [], []; monkeypatch.setattr(fg, "http", fake_http(pages, calls))
    cl = app.app.test_client(); fid = scene(studio="BadMILFS", cast="JC Wilds, Artemisia Love")
    c = db.connect(); db.vg_learned_set(c, "Bad MILFs", "AdultTime", "AdultTime", 0, "old", "https://vipergirls.to/threads/1")
    cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/threads/7285707-x", "manual": True})
    r = cl.get("/api/vg-learned").json["rules"]
    assert len(r) == 1 and r[0]["prefix"] == "BadMILFS" and r[0]["names_in_title"] == 1 and "7285707" in r[0]["source_url"]
    assert cl.delete("/api/vg-learned?key=" + r[0]["studio_key"]).json["removed"] == 1
    assert cl.get("/api/vg-learned").json["rules"] == []

def test_review_scene_still_blocked(monkeypatch):
    pages, calls = [], []; monkeypatch.setattr(fg, "http", fake_http(pages, calls))
    cl = app.app.test_client(); fid = scene(notes="Review by hand")
    r = cl.post(f"/api/films/{fid}/findgallery/download", json={"url": "https://vipergirls.to/threads/9100", "manual": True})
    assert r.status_code == 403 and pages == [] and db.vg_learned_list(db.connect()) == []
