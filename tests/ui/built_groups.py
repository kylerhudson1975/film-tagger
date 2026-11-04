"""Headless check (needs playwright + Chrome; not part of the plain test run): Built profiles 'Group by: Studio / label'
shows collapsible sections (name, performers, scenes; most scenes first), cards with the scene count for that studio plus
the total, open/closed state and the Group by choice remembered across reloads, search narrowing sections and cards, and
Order groups (Most scenes, A–Z ignoring case/accents/leading "The", Z–A, Most performers; remembered,
only shown when grouped), and Group by: None back to the plain grid. Temp library, text only, images off.
  python tests/ui/built_groups.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db, studios
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
spec = {"Cora Chase": [("Taboo Heat", 6), ("Jerky Wives", 4), ("Solo Site", 3)],
        "Mia Jay": [("Jerky Wives", 5), ("Solo Site", 1)],
        "Amy Cam": [("Amiees Fetish House", 3), ("Taboo Heat", 3)],
        "Ela Elan": [("Élan Films", 3), ("The Zebra Co", 4)]}
for i, n in enumerate(spec, 1):
    c.execute("INSERT INTO films(id,title,kind,cast_,studio,release_date,year) VALUES(?,?,?,?,?,?,?)", (i, f"L{i}", "scene", n, "Lib Studio", "2022-01-01", 2022))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, f"D:\\S\\L{i}.mp4"))
c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in spec}
for n, sl in spec.items():
    lst = [dict(id=f"{n[:2]}{st[:3]}{k}", _id="", title="S", date="2020-01-01", studio=st, slug=None, thumb=None) for st, m in sl for k in range(m)]
    c.execute("UPDATE people SET birthday='1985-01-01', tpdb_scenes_json=?, tpdb_scenes_at=1 WHERE id=?", (json.dumps(lst), ids[n]))
c.executescript(studios.SCHEMA)
c.execute("INSERT INTO studio_info(key,name,tpdb_parent) VALUES('tabooheat','Taboo Heat','Cory Chase Network')")
db.add_studio_link(c, "Jerky Wives", "Cory Chase Network"); c.commit()
srv = make_server("127.0.0.1", 5100, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5100/"); pg.wait_for_timeout(1500)
    show = "setIn('people');S.ppFav=false;S.ppUnop=false;S.ppBuilt=true;load()"
    pg.evaluate("localStorage.removeItem('bGroup');localStorage.removeItem('bgOpen');localStorage.removeItem('bSort');S.bGroup='';S.bgOpen={};S.bSort={k:'busiest_n',d:-1};" + show); pg.wait_for_timeout(1200)
    check(pg.locator("#ppBuilt .bgrp").count() == 0 and pg.locator("#ppBuilt .bcard").count() == 4, "starts ungrouped")
    check(pg.locator("#ppBgOrd").count() == 0, "Order groups only shown when grouped")
    pg.select_option("#ppBGroupSel", "label"); pg.wait_for_timeout(1200)
    heads = pg.evaluate("[...document.querySelectorAll('#ppBuilt .bgh')].map(x=>x.innerText.replace(/\\s+/g,' ').trim())")
    check(heads[:1] == ["▾ Cory Chase Network 3 performers · 18 scenes"], f"first section {heads}")   # Cora 10, Mia 5, Amy 3
    names = [h.split(" ", 1)[1] for h in heads]
    check(any(h.startswith("Solo Site") for h in names) and not any(h.startswith("Taboo Heat") for h in names), f"sections {heads}")
    check(pg.input_value("#ppBSortSel") == "here", "sorted by scenes with this studio")
    first = lambda: pg.evaluate("[...document.querySelectorAll('#ppBuilt .bgrp')][0].querySelectorAll('.bcard').length ? [...[...document.querySelectorAll('#ppBuilt .bgrp')][0].querySelectorAll('.bcard')].map(x=>x.querySelector('.bname').childNodes[0].textContent.trim()+' | '+x.querySelector('.bhere').innerText.trim()) : []")
    check(first() == ["Cora Chase | 10 scenes with Cory Chase Network · 14 total", "Mia Jay | 5 scenes with Cory Chase Network · 7 total", "Amy Cam | 3 scenes with Cory Chase Network · 7 total"], f"cards {first()}")
    pg.click("#ppBuilt .bgrp >> nth=0 >> .bgh"); pg.wait_for_timeout(300)   # collapse the first section
    check(first() == [] and "▸" in pg.inner_text("#ppBuilt .bgrp >> nth=0 >> .bgh"), "collapsed")
    pg.reload(); pg.wait_for_timeout(1500); pg.evaluate(show); pg.wait_for_timeout(1500)
    check(pg.input_value("#ppBGroupSel") == "label", "Group by remembered")
    check(first() == [], "collapsed state remembered")
    pg.fill("#q", "mia"); pg.evaluate("load()"); pg.wait_for_timeout(1200)
    secs = pg.evaluate("[...document.querySelectorAll('#ppBuilt .bgrp')].map(g=>g.querySelector('.bgh b').textContent+':'+[...g.querySelectorAll('.bname')].map(x=>x.childNodes[0].textContent.trim()).join(','))")
    check(secs == ["Cory Chase Network:Mia Jay"], f"search narrows sections {secs}")   # Mia: 1 Solo Site scene, not listed there
    pg.fill("#q", ""); pg.evaluate("load()"); pg.wait_for_timeout(1000)
    pg.click("#ppBgExp"); pg.wait_for_timeout(300)
    check(pg.locator("#ppBuilt .bgrp .pcards").count() == pg.locator("#ppBuilt .bgrp").count(), f"expand all {pg.locator('#ppBuilt .bgrp .pcards').count()} {pg.locator('#ppBuilt .bgrp').count()} {pg.evaluate("JSON.stringify(S.bgOpen)")}")
    gnames = lambda: pg.evaluate("[...document.querySelectorAll('#ppBuilt .bgh b')].map(x=>x.textContent)")
    check(pg.input_value("#ppBgOrd") == "scenes" and gnames()[:3] == ["Cory Chase Network", "Solo Site", "The Zebra Co"], f"most scenes {gnames()}")
    check("ordered by most scenes" in pg.inner_text("#ppBgNote"), "note names the order")
    pg.select_option("#ppBgOrd", "az"); pg.wait_for_timeout(300)
    check(gnames() == ["Amiees Fetish House", "Cory Chase Network", "Élan Films", "Solo Site", "The Zebra Co"], f"A-Z {gnames()}")
    check("ordered A–Z" in pg.inner_text("#ppBgNote"), "note says A-Z")
    pg.select_option("#ppBgOrd", "za"); pg.wait_for_timeout(300)
    check(gnames() == ["The Zebra Co", "Solo Site", "Élan Films", "Cory Chase Network", "Amiees Fetish House"], f"Z-A {gnames()}")
    pg.select_option("#ppBgOrd", "people"); pg.wait_for_timeout(300)
    check(gnames() == ["Cory Chase Network", "Solo Site", "The Zebra Co", "Amiees Fetish House", "Élan Films"], f"most performers {gnames()}")
    pg.select_option("#ppBgOrd", "az"); pg.reload(); pg.wait_for_timeout(1500); pg.evaluate(show); pg.wait_for_timeout(1500)
    check(pg.input_value("#ppBgOrd") == "az" and gnames()[0] == "Amiees Fetish House", "order remembered")
    pg.select_option("#ppBGroupSel", ""); pg.wait_for_timeout(500)
    check(pg.locator("#ppBuilt .bgrp").count() == 0 and pg.locator("#ppBuilt .bcard").count() == 4 and pg.input_value("#ppBSortSel") == "busiest_n", "Group by None: plain grid again")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
