"""Headless check (needs playwright + Chrome; not part of the plain test run): People → Built profiles is a photo-card grid
(initials when no photo), each card showing total scenes, busiest year count + year, years active and average per year,
a fuchsia ★ on favorites, a Sort by menu with the old table's sort keys (default: scenes in busiest year, high to low)
plus a direction button, and the search box narrowing the cards. Temp library, text only, images off.
  python tests/ui/built_cards.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
people = {"Ana Alpha": [2010, 2010, 2010, 2011], "Bo Beta": [2015, 2016, 2016, 2017, 2018, 2019], "Cy Gamma": [2020, 2020]}
for i, n in enumerate(people, 1):
    c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(?,?,?,?,?,?)", (i, f"T{i}", "scene", n, f"{people[n][-1]}-06-01", people[n][-1]))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\T{i}.mp4"))
c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in people}
for n, ys in people.items():
    js = json.dumps([dict(id=f"{n[:2]}{k}", _id="", title=f"S{k}", date=f"{y}-01-01", studio="S", slug=None, thumb=None) for k, y in enumerate(ys)])
    c.execute("UPDATE people SET birthday='1980-01-01', tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?", (js, ids[n]))
c.commit()
cl.post(f"/api/people/{ids['Bo Beta']}/favorite", json={"favorite": True})
srv = make_server("127.0.0.1", 5099, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5099/"); pg.wait_for_timeout(1500)
    pg.evaluate("localStorage.removeItem('bSort');S.bSort={k:'busiest_n',d:-1};setIn('people');S.ppFav=false;S.ppUnop=false;S.ppBuilt=true;load()"); pg.wait_for_timeout(1200)
    names = lambda: pg.evaluate("[...document.querySelectorAll('#ppBuilt .bcard .bname')].map(x=>x.childNodes[0].textContent.trim())")
    check(pg.locator("#ppBuilt table").count() == 0, "no table any more")
    check(names() == ["Ana Alpha", "Cy Gamma", "Bo Beta"], f"default sort (busiest year count, high first) {names()}")  # 3 / 3 / 2 (tie: A-Z)
    check(pg.input_value("#ppBSortSel") == "busiest_n", "menu shows the default sort")
    opts = pg.evaluate("[...document.querySelectorAll('#ppBSortSel option')].map(o=>o.value)")
    check(sorted(opts) == sorted(["busiest_n", "total", "busiest_year", "career", "years", "avg", "name"]), f"sort options {opts}")
    t = pg.inner_text("#ppBuilt")
    for want in ("Total", "Busiest", "3 in 2010", "Active", "2010–2011", "(2 yrs)", "Avg / yr", "2015–2019", "(5 yrs)"):
        check(want in t, f"card text missing {want!r}")
    check(pg.evaluate("[...document.querySelectorAll('#ppBuilt .bcard')].filter(x=>x.querySelector('.pfs.st-on')).map(x=>x.querySelector('.bname').childNodes[0].textContent.trim())") == ["Bo Beta"], "only Bo has the star")
    check(pg.evaluate("getComputedStyle(document.querySelector('#ppBuilt .pfs.st-on')).color") == "rgb(255, 0, 255)", "star is fuchsia")
    check(pg.evaluate("[...document.querySelectorAll('#ppBuilt .pph span')].length") == 3, "initials shown when there is no photo")
    check(pg.evaluate("getComputedStyle(document.querySelector('#ppBuilt .bname')).color") == "rgb(230, 232, 235)", "name in the light text color")
    pg.select_option("#ppBSortSel", "total"); pg.wait_for_timeout(200)
    check(names() == ["Bo Beta", "Ana Alpha", "Cy Gamma"], f"total sort {names()}")
    pg.click("#ppBDirBtn"); pg.wait_for_timeout(200)
    check(names() == ["Cy Gamma", "Ana Alpha", "Bo Beta"], f"total low-high {names()}")
    pg.select_option("#ppBSortSel", "name"); pg.wait_for_timeout(200)
    check(names() == ["Ana Alpha", "Bo Beta", "Cy Gamma"] and "A–Z" in pg.inner_text("#ppBDirBtn"), f"name sort {names()}")
    check(pg.evaluate("JSON.parse(localStorage.bSort).k") == "name", "sort remembered")
    pg.fill("#q", "bo"); pg.evaluate("load()"); pg.wait_for_timeout(900)
    check(names() == ["Bo Beta"] and "1 of 3 match" in pg.inner_text("#ppBuilt"), f"search narrows {names()}")
    pg.click("#ppBuilt .bcard"); pg.wait_for_timeout(900)
    check(pg.evaluate("S.page") == f"person:{ids['Bo Beta']}", "card opens the profile")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
