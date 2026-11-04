"""Headless check (playwright + Chrome; text only, images off; mainstream test data): on the person page, clicking a
year's blue (in-library) bar in Insights > Career timeline filters the in-library credits to that year (chip
'YEAR (n) ×'); the same bar again or the × clears it; empty years aren't clickable; no separate per-year chart;
the filter resets for the next person.
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
    bar = lambda y: f"#ppIns g.dsh-bar:has(title:text-matches('^{y}:'))"
    pg.evaluate("localStorage.ppInsOpen='1';localStorage.ppInsChart='bar'")
    pg.evaluate(f"showPerson({ids['Max Main']})"); pg.wait_for_timeout(2500)
    check(pg.locator("#ppYears").count() == 0 and "Titles per year" not in pg.inner_text("#content"), "no separate per-year chart")
    tips = pg.evaluate("[...document.querySelectorAll('#ppIns svg g')].filter(g=>g.querySelector('title')).map(g=>g.getAttribute('class')+'|'+g.querySelector('title').textContent)")
    check(any(t.startswith("dsh-bar|2001:") for t in tips) and any(t.startswith("dsh-nb|2002:") for t in tips), f"blue bars clickable, empty year not {tips}")
    check(pg.evaluate("getComputedStyle(document.querySelector('#ppIns g.dsh-bar')).cursor") == "pointer", "pointer cursor on blue bars")
    check(sorted(titles()) == ["Alpha", "Beta", "Delta", "Gamma"], f"all titles {titles()}")
    pg.click(bar(2001)); pg.wait_for_timeout(500)
    check(sorted(titles()) == ["Alpha", "Beta"] and pg.inner_text("#ppYearChip").startswith("2001 (2)"), f"2001 filter {titles()}")
    check(pg.evaluate("String(S.page)").startswith("person:"), "stays on the person page")
    pg.click(bar(2001)); pg.wait_for_timeout(500)
    check(len(titles()) == 4 and pg.locator("#ppYearChip").count() == 0, "same bar again clears")
    pg.click(bar(2003)); pg.wait_for_timeout(500); check(titles() == ["Gamma"], f"2003 filter {titles()}")
    pg.click("#ppYearChip a"); pg.wait_for_timeout(500); check(len(titles()) == 4 and pg.locator("#ppYearChip").count() == 0, "chip × clears")
    pg.click(bar(2003)); pg.wait_for_timeout(300)
    pg.evaluate(f"showPerson({ids['Una Dated']})"); pg.wait_for_timeout(1500)
    check(pg.locator("#ppYearChip").count() == 0 and sorted(titles()) == ["Delta", "Eps"], f"filter reset for the next person {titles()}")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
