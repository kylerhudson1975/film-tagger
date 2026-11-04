"""Headless check (needs playwright + Chrome; not part of the plain test run): the top-level Performers page. Top-bar button
between Home and Library & Settings (active style), PERFORMERS header, search, tabs with counts (Favorites, Built profiles,
Not opened yet, All people, Studios), cards open a side summary panel without leaving the list (stats, busiest year,
timeline, top studios, co-stars, star toggle, Open full profile), Esc closes it, an unopened person shows 'Building…' and
updates when the background build ends, the Studios tab lists labels with their rosters, the last tab is remembered, Back
returns to it, and Home / Dashboard / the old People lists still work. Temp library, text only, images off.
  python tests/ui/performers.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db, studios, tpdb
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "tpdb_key", "t")
spec = {"Cora Chase": [("Taboo Heat", 6, 2019), ("Jerky Wives", 4, 2018), ("Solo Site", 3, 2017)],
        "Mia Jay": [("Jerky Wives", 5, 2018), ("Solo Site", 1, 2016)],
        "Amy Cam": [("Amiees Fetish House", 3, 2015), ("Taboo Heat", 3, 2016)]}
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(1,'L1','scene','Cora Chase, Mia Jay','Taboo Heat','2020-01-01',2020)")
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(2,'L2','scene','Amy Cam','Lib Studio','2020-01-01',2020)")
c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(3,'L3','scene','Tess New','Lib Studio','2012-01-01',2012)")
for i in (1, 2, 3): c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, f"D:\\S\\L{i}.mp4"))
c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in list(spec) + ["Tess New"]}
for n, sl in spec.items():
    lst = [dict(id=f"{n[:2]}{st[:3]}{k}", _id="", title="S", date=f"{y}-01-01", studio=st, slug=None, thumb=None) for st, m, y in sl for k in range(m)]
    c.execute("UPDATE people SET birthday='1985-01-01', tpdb_scenes_json=?, tpdb_scenes_at=1, fetched_at=1 WHERE id=?", (json.dumps(lst), ids[n]))
c.execute("UPDATE people SET birthday='1990-01-01' WHERE id=?", (ids["Tess New"],))
c.executescript(studios.SCHEMA)
c.execute("INSERT INTO studio_info(key,name,tpdb_parent) VALUES('tabooheat','Taboo Heat','Cory Chase Network')")
db.add_studio_link(c, "Jerky Wives", "Cory Chase Network"); c.commit()
gate = threading.Event()
def tp(path, tok, **k):   # stand-in ThePornDB for Tess New's background build (page 2 waits for the test)
    if path == "/performers": return {"data": [{"id": "tn", "name": "Tess New", "extras": {}}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1)
        if p == 2: gate.wait(30)
        return {"data": [{"id": f"t{i}", "title": f"T{i}", "date": f"{2013 + i % 3}-01-01", "site": {"name": "Tess Site"}} for i in range((p - 1) * 100, min(p * 100, 150))],
                "meta": {"total": 150, "last_page": 2, "current_page": p}}
    return {"data": {"id": "tn", "name": "Tess New", "extras": {}}}
tpdb.call_retry = tp
srv = make_server("127.0.0.1", 5101, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1500, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5101/"); pg.wait_for_timeout(1500)
    pg.evaluate("localStorage.removeItem('pfTab');localStorage.removeItem('pfSGroup');localStorage.removeItem('bgOpen');S.bgOpen={};S.pfTab='fav'")
    nav = pg.evaluate("[...document.querySelectorAll('#navRow button')].map(b=>b.innerText.trim())")
    check(nav[:4] == ["Dashboard", "Home", "Performers", "Library & Settings"], f"nav order {nav}")
    pg.click("#perfBtn"); pg.wait_for_timeout(1500)
    check(pg.evaluate("S.page") == "perf" and "on" in pg.get_attribute("#perfBtn", "class"), "Performers page shown, button active")
    check(pg.evaluate("[...document.querySelectorAll('#navRow button.on')].map(b=>b.id)") == ["perfBtn"], "only Performers active")
    check(pg.inner_text(".pf-h") == "PERFORMERS" and pg.get_attribute("#pfQ", "placeholder") == "Search performers…", "header + search")
    tabs = lambda: pg.evaluate("[...document.querySelectorAll('#pfTabs button')].map(b=>b.innerText.replace(/\\s+/g,' ').trim())")
    t = tabs()
    check(t[0] == "★ Favorites (0)" and t[1] == "Built profiles (3)" and t[2].startswith("Not opened yet") and t[3] == "All people" and t[4].startswith("Studios"), f"tabs {t}")
    check(pg.locator("#pfTabs button.on").get_attribute("data-tab") == "fav" and pg.locator("#ppFav").count() == 1, "Favorites tab first")
    pg.click("#pfTabs button[data-tab=built]"); pg.wait_for_timeout(1200)
    names = pg.evaluate("[...document.querySelectorAll('#pfList .bcard .bname')].map(x=>x.childNodes[0].textContent.trim())")
    check(sorted(names) == ["Amy Cam", "Cora Chase", "Mia Jay"], f"built cards {names}")
    check(pg.locator("#ppBSortSel").count() == 1 and pg.locator("#ppBGroupSel").count() == 1, "sort + group controls")
    pg.click("#pfList .bcard[data-pid='%d']" % ids["Cora Chase"]); pg.wait_for_timeout(1200)
    check(pg.is_visible("#pfPanel") and pg.evaluate("S.page") == "perf", "panel opens, still on the list")
    check(pg.locator("#pfList .bcard").count() == 3, "list kept")
    check(pg.inner_text(".pf-name").startswith("Cora Chase"), "panel name")
    check(pg.inner_text("#pfStats") == "14 scenes · 2017–2020", f"stats {pg.inner_text('#pfStats')}")
    check(pg.inner_text("#pfBusy") == "Busiest: 2019 (6)", f"busiest {pg.inner_text('#pfBusy')}")
    check(pg.locator("#pfTl svg").count() == 1, "timeline chart")
    st = pg.evaluate("[...document.querySelectorAll('#pfStudios div')].map(x=>x.innerText.replace(/\\s+/g,' ').trim())")
    check(st[0] == "Taboo Heat 7" and len(st) == 3, f"top studios {st}")
    check("Mia Jay" in pg.inner_text("#pfCostars"), f"co-stars {pg.inner_text('#pfCostars')}")
    check("sel" in pg.get_attribute("#pfList .bcard[data-pid='%d']" % ids["Cora Chase"], "class"), "selected card marked")
    check(pg.get_attribute("#pfFavBtn", "aria-pressed") == "false" and pg.locator("#pfFavBtn .st-off").count() == 1, "hollow star")
    pg.click("#pfFavBtn"); pg.wait_for_timeout(800)
    check(pg.get_attribute("#pfFavBtn", "aria-pressed") == "true" and pg.locator("#pfFavBtn .st-on").count() == 1, "star toggled on")
    check(tabs()[0] == "★ Favorites (1)", f"fav count {tabs()[0]}")
    check(pg.locator("#pfList .bcard[data-pid='%d'] .st-on" % ids["Cora Chase"]).count() == 1, "card shows the fuchsia star")
    pg.keyboard.press("Escape"); pg.wait_for_timeout(300)
    check(not pg.is_visible("#pfPanel") and pg.evaluate("S.page") == "perf", "Esc closes the panel")
    pg.click("#pfList .bcard[data-pid='%d']" % ids["Mia Jay"]); pg.wait_for_timeout(1000)
    pg.click(".pf-x"); pg.wait_for_timeout(300); check(not pg.is_visible("#pfPanel"), "✕ closes the panel")
    pg.fill("#pfQ", "amy"); pg.wait_for_timeout(500)
    names = pg.evaluate("[...document.querySelectorAll('#pfList .bcard .bname')].map(x=>x.childNodes[0].textContent.trim())")
    check(names == ["Amy Cam"], f"search narrows {names}"); pg.fill("#pfQ", ""); pg.wait_for_timeout(400)
    # Studios
    pg.click("#pfTabs button[data-tab=studios]"); pg.wait_for_timeout(1500)
    opts = pg.evaluate("[...document.querySelectorAll('#ppBGroupSel option')].map(o=>o.value)")
    check("" not in opts and pg.input_value("#ppBGroupSel") == "label", f"studios: label grouping, no None {opts}")
    heads = pg.evaluate("[...document.querySelectorAll('#pfList .bgh b')].map(x=>x.textContent)")
    check(heads[:1] == ["Cory Chase Network"] and pg.locator("#ppBgOrd").count() == 1, f"studio list {heads}")
    check(pg.locator("#pfTabs button[data-tab=studios]").inner_text().strip().endswith(f"({len(heads)})"), "studios count")
    pg.select_option("#ppBgOrd", "az"); pg.wait_for_timeout(300)
    h2 = pg.evaluate("[...document.querySelectorAll('#pfList .bgh b')].map(x=>x.textContent)")
    check(h2 == sorted(h2, key=str.lower), f"A-Z {h2}")
    pg.click("#ppBgExp"); pg.wait_for_timeout(300)
    roster = pg.evaluate("[...document.querySelectorAll('#pfList .bgrp')].find(g=>g.querySelector('.bgh b').textContent=='Cory Chase Network').querySelectorAll('.bcard').length")
    check(roster == 3, f"roster {roster}")
    check(pg.evaluate("S.bGroup") == "", "Built tab grouping choice untouched")
    # Not opened yet -> building state -> updates
    pg.click("#pfTabs button[data-tab=unop]"); pg.wait_for_timeout(1500)
    rows = pg.evaluate("[...document.querySelectorAll('#ppUnop tr[data-pid]')].map(r=>+r.dataset.pid)")
    check(ids["Tess New"] in rows, f"unopened rows {rows}")
    if ids["Tess New"] in rows:
        pg.click("#ppUnop tr[data-pid='%d']" % ids["Tess New"]); pg.wait_for_timeout(2500)
        check(pg.is_visible("#pfBuild"), "building state shown")
        gate.set(); pg.wait_for_timeout(5000)
        check(pg.locator("#pfBuild").count() == 0 and pg.inner_text("#pfStats").startswith("151 scenes"), f"panel updated {pg.inner_text('#pfStats')}")
        check(pg.evaluate("S.page") == "perf", "still on Performers")
        check(pg.locator("#pfTabs button[data-tab=built]").inner_text().strip() == "Built profiles (4)", "built count follows the finished build")
    gate.set()
    # All people
    pg.click("#pfTabs button[data-tab=all]"); pg.wait_for_timeout(500)
    check("Type a name" in pg.inner_text("#pfAll"), "all: prompt")
    pg.fill("#pfQ", "mia"); pg.wait_for_timeout(1200)
    check(pg.locator("#pfAll .pcard").count() == 1 and "Mia Jay" in pg.inner_text("#pfAll"), f"all people search {pg.inner_text('#pfAll')}")
    pg.click("#pfAll .pcard"); pg.wait_for_timeout(1000)
    check(pg.inner_text(".pf-name").startswith("Mia Jay"), "all: panel")
    # Open full profile, then Back
    pg.click("#pfOpenBtn"); pg.wait_for_timeout(1500)
    check(pg.evaluate("String(S.page)") == f"person:{ids['Mia Jay']}" and "on" not in (pg.get_attribute("#perfBtn", "class") or ""), "full profile opened")
    pg.go_back(); pg.wait_for_timeout(1500)
    check(pg.evaluate("S.page") == "perf" and pg.locator("#pfTabs button.on").get_attribute("data-tab") == "all", "Back returns to the Performers tab")
    # remembered tab
    pg.reload(); pg.wait_for_timeout(1500); pg.evaluate("goHome()"); pg.wait_for_timeout(800)
    check(pg.evaluate("S.page") is None and "on" not in (pg.get_attribute("#perfBtn", "class") or ""), "Home: library")
    pg.click("#perfBtn"); pg.wait_for_timeout(1200)
    check(pg.locator("#pfTabs button.on").get_attribute("data-tab") == "all", "last tab remembered")
    pg.click("#dashBtn"); pg.wait_for_timeout(1200)
    check(pg.evaluate("S.page") == "dash" and pg.evaluate("[...document.querySelectorAll('#navRow button.on')].map(b=>b.id)") == ["dashBtn"], "Dashboard unchanged")
    # old People section in Porn Scenes still works
    pg.evaluate("setIn('people');S.ppFav=false;S.ppUnop=false;S.ppBuilt=true;load()"); pg.wait_for_timeout(1500)
    check(pg.locator("#ppBuilt .bcard").count() == 4, "old Built profiles list (Tess New now built too)")
    pg.click("#ppBuilt .bcard[data-pid='%d']" % ids["Amy Cam"]); pg.wait_for_timeout(1200)
    check(pg.evaluate("String(S.page)") == f"person:{ids['Amy Cam']}", "old card opens the person page")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
