"""Headless check (playwright + Chrome; text only, images off): Needs ethnicity shows Wikidata conflict
listings next to the person and a one-click tag is saved as a locked hand edit. Temp library.
python tests/ui/ethnicity_conflict.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app, people as P
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"])
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(1,'T1','scene','Quinn Conflict','2015-01-01',2015)")
c.execute("INSERT INTO files(film_id,path) VALUES(1, ?)", (rf"D:\S\1.mp4",))
c.commit(); cl = app.app.test_client()
qid = cl.get("/api/person/lookup?name=Quinn Conflict").json["id"]
c.execute("UPDATE people SET ethnicity=?, ethnicity_source=?, favorite=1, favorite_at=1 WHERE id=?",
          ("Ashkenazi Jews, Irish Americans", "wikidata Q2", qid)); c.commit()
assert P.apply_ancestry_fold(c)[1] == 1
srv = make_server("127.0.0.1", 5108, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1500, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5108/"); pg.wait_for_timeout(1000)
    pg.click("#perfBtn"); pg.wait_for_timeout(800)
    pg.click("#pfCatP"); pg.wait_for_timeout(800)
    pg.click("#pfTabs button[data-tab=neth]"); pg.wait_for_timeout(1000)
    check(pg.locator("#nethName").inner_text() == "Quinn Conflict", "conflict person is first in Needs ethnicity")
    txt = pg.locator("#nethListings").inner_text() if pg.locator("#nethListings").count() else ""
    check("Wikidata lists:" in txt and "Ashkenazi Jews" in txt and "Irish Americans" in txt, f"listings next to photo: {txt!r}")
    # Keys 1-9 skip Jewish: 9 is White
    pg.keyboard.press("9"); pg.wait_for_timeout(900)
    row = c.execute("SELECT ethnicity, ethnicity_source, edited_fields FROM people WHERE id=?", (qid,)).fetchone()
    check(row["ethnicity"] == "White" and row["ethnicity_source"] == "manual" and "ethnicity" in (row["edited_fields"] or ""), f"tag locked {tuple(row)}")
    check("Nobody left" in pg.inner_text("#pfNeth") or pg.locator("#nethName").count() == 0, "left the queue after tag")
    br.close()
if errs: fails.extend(errs)
if fails:
    print("FAIL"); [print("-", x) for x in fails]; raise SystemExit(1)
print("ok")
