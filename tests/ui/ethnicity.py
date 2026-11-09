"""Headless check (playwright + Chrome; text only, images off): the People page Ethnicity filter. It is there on the opening
screen and stays there (with that side's counts) after picking Mainstream or Porn and on every tab; picking a value lists only
people with it; switching Mainstream / Porn keeps the choice; tabs other than All people are narrowed to it too; the People
button clears it. Temp library.  python tests/ui/ethnicity.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
P = [("Ana Porn", "scene", "Latina/Latino"), ("Bea Porn", "scene", "White"), ("Cat Porn", "scene", "White"),
     ("Max Main", "movie", "White"), ("Moe Main", "movie", "Black"), ("Nia Main", "movie", None)]
for i, (n, k, e) in enumerate(P, 1):
    c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(?,?,?,?,?,?)", (i, f"T{i}", k if k == "scene" else None, n, "2015-01-01", 2015))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, rf"D:\S\{i}.mp4" if k == "scene" else rf"D:\Movies\{i}.mkv"))
c.commit(); cl = app.app.test_client()
ids = {}
for n, k, e in P:
    ids[n] = cl.get("/api/person/lookup?name=" + n).json["id"]
    c.execute("UPDATE people SET ethnicity=?, ethnicity_source='test', favorite=1, favorite_at=1 WHERE id=?", (e, ids[n])); c.commit()
c.commit()
srv = make_server("127.0.0.1", 5107, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1500, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    vis = lambda: pg.locator("#pfEth").count() == 1 and pg.locator("#pfEth").is_visible()
    opts = lambda: [o for o in pg.locator("#pfEth option").all_inner_texts() if not o.startswith("──")]
    names = lambda: sorted(set(pg.locator("#pfList .pcard b, #pfList tr b, #pfList [data-pid] b").all_inner_texts()))
    pg.goto("http://127.0.0.1:5107/"); pg.wait_for_timeout(1200)
    pg.click("#perfBtn"); pg.wait_for_timeout(1000)
    check(vis(), "dropdown on the opening screen")
    check(opts() == ["Any ethnicity", "White (3)", "Black (1)", "Latina/Latino (1)"], f"opening counts {opts()}")
    pg.click("#pfCatP"); pg.wait_for_timeout(1000)
    check(vis(), "dropdown after Porn")
    check(opts() == ["Any ethnicity", "White (2)", "Latina/Latino (1)"], f"porn counts {opts()}")
    pg.select_option("#pfEth", "White"); pg.wait_for_timeout(1000)
    check(vis(), "dropdown after picking in Porn")
    check(names() == ["Bea Porn", "Cat Porn"], f"porn White list {names()}")
    pg.click("#pfCatM"); pg.wait_for_timeout(1000)
    check(vis(), "dropdown after Mainstream")
    check(pg.locator("#pfEth").input_value() == "White", "choice kept on switching side")
    check(opts() == ["Any ethnicity", "White (1)", "Black (1)"], f"mainstream counts {opts()}")
    check(names() == ["Max Main"], f"mainstream White list {names()}")
    for t in ("fav", "built", "all"):
        b = pg.locator(f"#pfTabs button[data-tab={t}]")
        if b.count():
            b.click(); pg.wait_for_timeout(900); check(vis(), f"dropdown on tab {t}")
            if t == "fav": check(names() == ["Max Main"], f"Favorites narrowed {names()}")
    pg.click("#perfBtn"); pg.wait_for_timeout(1000)
    check(vis() and pg.locator("#pfEth").input_value() == "", "People button clears the filter")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL:\n" + "\n".join(fails) if fails else "ok")
