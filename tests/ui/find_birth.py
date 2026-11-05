"""Headless check (playwright + Chrome; text only, images off): 'Find birth date' on a Needs attention card, in the side panel
and in Edit profile; 'Find all birth dates' with the birth filter; the review popup shows candidates with flags, Accept is
disabled for impossible / year-only ones, Accept saves through Edit profile, Skip does nothing. The lookup endpoint is mocked.
  python tests/ui/find_birth.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
for i, n in enumerate(["Ann Old", "Bea Two"], 1):
    c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(?,?,?,?,?,?)", (i, f"T{i}", "scene", n, "1978-05-01", 1978))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\{i}.mp4"))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Ann Old", "Bea Two")}
def res(pid, name):
    return {"id": pid, "name": name, "titles": 1, "first_title": {"title": "T1", "date": "1978-05-01"}, "conflict": True, "errors": [], "candidates": [
        {"source": "ThePornDB", "url": "https://theporndb.net/performers/x", "date": "1960-03-01", "year": 1960, "matched": name, "age_first": 18, "impossible": False, "year_only": False, "conflict": True},
        {"source": "TMDB", "url": "https://www.themoviedb.org/person/5", "date": "1962-01-01", "year": 1962, "matched": name, "age_first": 16, "impossible": True, "why": "would be 16 at “T1” (1978-05-01)", "year_only": False, "conflict": True},
        {"source": "Wikidata", "url": "https://www.wikidata.org/wiki/Q1", "date": None, "year": 1959, "matched": name, "age_first": 18, "impossible": False, "year_only": True, "conflict": True}]}
srv = make_server("127.0.0.1", 5103, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1500, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    looked = []
    def route(r):
        pid = int(r.request.url.split("/api/people/")[1].split("/")[0]); looked.append(pid)
        r.fulfill(status=200, content_type="application/json", body=json.dumps(res(pid, "Ann Old" if pid == ids["Ann Old"] else "Bea Two")))
    pg.route("**/find_birth", route)
    pg.goto("http://127.0.0.1:5103/"); pg.wait_for_timeout(1500)
    pg.click("#perfBtn"); pg.wait_for_timeout(800); pg.click("#pfCatP"); pg.wait_for_timeout(1000)
    pg.click("#pfTabs button[data-tab=need]"); pg.wait_for_timeout(1200)
    check(pg.locator("#pfNeed .fb-btn").count() == 2, "Find birth date on cards with No birth date")
    pg.click(f"#pfNeed .fb-btn[data-fb='{ids['Ann Old']}']"); pg.wait_for_timeout(800)
    check(not pg.is_visible("#pfPanel"), "card button does not open the panel")
    rows = pg.locator("#fbBox tr[data-src]")
    check(rows.count() == 3, f"3 candidates {rows.count()}")
    acc = lambda s: pg.locator(f"#fbBox tr[data-src='{s}'] .fb-acc")
    check(acc("ThePornDB").is_enabled() and acc("TMDB").is_disabled() and acc("Wikidata").is_disabled(), "Accept only for a possible full date")
    t = pg.inner_text("#fbBox")
    check("Impossible: would be 16" in t and "Year only" in t and "Sources disagree" in t, "flags shown")
    check(c.execute("SELECT birthday FROM people WHERE id=?", (ids["Ann Old"],)).fetchone()[0] is None, "nothing saved before Accept")
    acc("ThePornDB").click(); pg.wait_for_timeout(1200)
    row = c.execute("SELECT birthday, birth_date_source, edited_fields FROM people WHERE id=?", (ids["Ann Old"],)).fetchone()
    check(row[0] == "1960-03-01" and "ThePornDB" in (row[1] or "") and "birthday" in json.loads(row[2] or "[]"), f"accepted as hand edit {tuple(row)}")
    check("Saved 1960-03-01" in pg.inner_text("#fbBox"), "popup shows saved")
    pg.click("#fbBox button:has-text('Close')"); pg.wait_for_timeout(1500)
    check(pg.locator("#pfNeed .fb-btn").count() == 1, "list refreshed: one left without a birth date")
    # Find all with the birth filter
    pg.click("#pfNeed .pf-need-f button[data-f=birth]"); pg.wait_for_timeout(300)
    looked.clear(); pg.click("#fbAll"); pg.wait_for_timeout(1200)
    check(looked == [ids["Bea Two"]] and pg.locator("#fbBox .fb-p").count() == 1, f"find all {looked}")
    pg.click("#fbBox .fb-skip"); pg.wait_for_timeout(200)
    check("Skipped" in pg.inner_text("#fbBox") and c.execute("SELECT birthday FROM people WHERE id=?", (ids["Bea Two"],)).fetchone()[0] is None, "skip saves nothing")
    pg.click("#fbBox button:has-text('Close')"); pg.wait_for_timeout(300)
    # side panel + Edit profile
    pg.click(f"#pfNeed .pcard[data-pid='{ids['Bea Two']}']"); pg.wait_for_timeout(1200)
    check(pg.locator("#pfFindBirth").count() == 1, "panel button")
    pg.click("#pfFindBirth"); pg.wait_for_timeout(800); check(pg.locator("#fbBox tr[data-src]").count() == 3, "panel lookup"); pg.click("#fbBox button:has-text('Close')")
    pg.click("#pfOpenBtn"); pg.wait_for_timeout(1500)
    check(pg.locator("#ppFindBirth").count() == 1, "profile page button")
    pg.click("#ppFindBirth"); pg.wait_for_timeout(800); check(pg.locator("#fbBox tr[data-src]").count() == 3, "profile lookup"); pg.click("#fbBox button:has-text('Close')")
    pg.evaluate("editPerson()"); pg.wait_for_timeout(300)
    check(pg.locator("#ppeFindBirth").count() == 1, "Edit profile button")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
