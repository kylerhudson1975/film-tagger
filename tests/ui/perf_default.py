"""Headless check (playwright + Chrome; text only, images off): the top-bar Performers button always opens the empty default
(no category, Porn/Mainstream not highlighted, 'Recently added' row for everyone): from another section, after picking Porn,
and after a page reload; only browser Back restores a category.  python tests/ui/perf_default.py"""
import os, sys, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); _c0.commit()
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "roots", [r"D:\Movies"])
c.execute("INSERT INTO films(id,title,kind,cast_,added_at) VALUES(1,'Sc','scene','Pia Porn',1790000000)"); c.execute("INSERT INTO files(film_id,path) VALUES(1,'D:\\S\\Sc.mp4')")
c.execute("INSERT INTO films(id,title,cast_,added_at) VALUES(2,'Mv','Max Main',1780000000)"); c.execute("INSERT INTO files(film_id,path) VALUES(2,'D:\\Movies\\Mv.mkv')")
c.commit(); cl = app.app.test_client()
for n in ("Pia Porn", "Max Main"): cl.get("/api/person/lookup?name=" + n)
srv = make_server("127.0.0.1", 5104, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1400, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    def empty(msg):
        check(pg.evaluate("S.page") == "perf" and pg.evaluate("S.pfCat") == "" and pg.locator(".pf-cats button.on").count() == 0
              and pg.locator("#pfEmpty").count() == 1 and not pg.is_visible("#pfTabs"), msg + f" (cat={pg.evaluate('S.pfCat')!r})")
        rc = pg.evaluate("[...document.querySelectorAll('#pfRecent .pf-rc b')].map(x=>x.textContent)")
        check(pg.locator("#pfRecent h4").inner_text().strip().lower() == "recently added" and rc == ["Max Main", "Pia Porn"], msg + f": recent row for everyone {rc}")
    pg.goto("http://127.0.0.1:5104/"); pg.wait_for_timeout(1500)
    pg.click("#dashBtn"); pg.wait_for_timeout(800); pg.click("#perfBtn"); pg.wait_for_timeout(1200); empty("from Dashboard")
    pg.click("#pfCatP"); pg.wait_for_timeout(1000); check(pg.evaluate("S.pfCat") == "porn", "Porn picked")
    pg.click("#perfBtn"); pg.wait_for_timeout(1200); empty("Performers again after Porn")
    pg.click("#pfCatP"); pg.wait_for_timeout(1000)
    pg.reload(); pg.wait_for_timeout(2000)
    if pg.evaluate("S.page") != "perf": pg.click("#perfBtn"); pg.wait_for_timeout(1200)
    empty("after reload")
    pg.click("#pfCatP"); pg.wait_for_timeout(800); pg.click("#homeBtn") if pg.locator("#homeBtn").count() else pg.evaluate("goHome()"); pg.wait_for_timeout(1000)
    pg.go_back(); pg.wait_for_timeout(1500)
    check(pg.evaluate("S.page") == "perf" and pg.evaluate("S.pfCat") == "porn", f"Back restores Porn ({pg.evaluate('S.pfCat')!r})")
    pg.click("#perfBtn"); pg.wait_for_timeout(1200); empty("Performers after Back")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
