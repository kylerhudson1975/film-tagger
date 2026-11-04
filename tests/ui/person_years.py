"""Headless check (playwright + Chrome; text only, images off; mainstream test data): the person page's yearly graph
counts their library titles per year (gaps as empty slots, undated left out), a bar click filters the in-library
credits to that year, clicking it again or the chip's × clears it; no chart without dated credits.
python tests/ui/person_years.py"""
import os, sys, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "roots", [r"D:\Movies"])
for i, t, y, cast in ((1, "Alpha", 2001, "Max Main"), (2, "Beta", 2001, "Max Main"), (3, "Gamma", 2003, "Max Main"), (4, "Delta", None, "Max Main, Una Dated"), (5, "Eps", None, "Una Dated")):
    c.execute("INSERT INTO films(id,title,year,cast_) VALUES(?,?,?,?)", (i, t, y, cast)); c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\Movies\{t}.mkv"))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Max Main", "Una Dated")}
srv = make_server("127.0.0.1", 5108, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1400, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5108/"); pg.wait_for_timeout(1500)
    titles = lambda: pg.evaluate("[...document.querySelectorAll('.pp .ppi .ppn')].map(x=>x.textContent)")
    pg.evaluate(f"showPerson({ids['Max Main']})"); pg.wait_for_timeout(1500)
    bars = pg.evaluate("[...document.querySelectorAll('#ppYears .ppyb')].map(g=>g.dataset.y+':'+g.querySelector('title').textContent.split(' ')[1])")
    check(bars == ["2001:2", "2002:0", "2003:1"], f"bars {bars}"); check(sorted(titles()) == ["Alpha", "Beta", "Delta", "Gamma"], f"all titles {titles()}")
    pg.click("#ppYears .ppyb[data-y='2001']"); pg.wait_for_timeout(400)
    check(sorted(titles()) == ["Alpha", "Beta"] and "2001 (2)" in pg.inner_text("#ppYearChip"), f"2001 filter {titles()}")
    check(pg.locator("#ppYears .ppyb.on").count() == 1, "selected bar marked")
    pg.click("#ppYears .ppyb[data-y='2001']"); pg.wait_for_timeout(400)
    check(len(titles()) == 4 and pg.locator("#ppYearChip").count() == 0, "click again clears")
    pg.click("#ppYears .ppyb[data-y='2003']"); pg.wait_for_timeout(400); check(titles() == ["Gamma"], f"2003 filter {titles()}")
    pg.click("#ppYearChip a"); pg.wait_for_timeout(400); check(len(titles()) == 4 and pg.locator("#ppYearChip").count() == 0, "chip × clears")
    pg.click("#ppYears .ppyb[data-y='2002']", force=True); pg.wait_for_timeout(300); check(pg.locator("#ppYearChip").count() == 0, "empty year not clickable")
    pg.click("#ppYears .ppyb[data-y='2003']"); pg.wait_for_timeout(300)
    pg.evaluate(f"showPerson({ids['Una Dated']})"); pg.wait_for_timeout(1500)
    check(pg.locator("#ppYears").count() == 0 and sorted(titles()) == ["Delta", "Eps"], f"no chart without dated credits; filter reset {titles()}")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
