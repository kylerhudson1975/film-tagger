"""Headless check (playwright + Chrome; text only, images off): Ethnicity in Edit profile and Add person
is a real <select> picker (plus a free-text field), not a datalist. Picking a group fills the field;
Save persists it; restoring the original value also saves. Temp library.
  python tests/ui/ethnicity_edit.py"""
import os, sys, json, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); c.commit()
c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(1,'T1','scene','Pat Test','2015-01-01',2015)")
c.execute("INSERT INTO files(film_id,path) VALUES(1,?)", (r"D:\S\1.mp4",)); c.commit()
cl = app.app.test_client()
pid = cl.get("/api/person/lookup?name=Pat Test").json["id"]
c.execute("UPDATE people SET ethnicity=?, ethnicity_source='test' WHERE id=?", ("White", pid)); c.commit()
srv = make_server("127.0.0.1", 5132, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
WANT = ["Asian","Black","Jewish","Latina/Latino","Middle Eastern","Mixed","Native American","Pacific Islander","South Asian","White"]
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"),
                            args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1500, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5132/"); pg.wait_for_timeout(800)
    # --- Edit profile ---
    pg.evaluate(f"showPerson({pid})"); pg.wait_for_timeout(1000)
    pg.evaluate("editPerson()"); pg.wait_for_timeout(300)
    check(pg.locator("#ppe_ethnicity").count()==1 and pg.locator("#ppe_ethnicity").is_visible(), "Edit profile ethnicity field")
    check(pg.locator("#ppe_eth_pick").count()==1 and pg.locator("#ppe_eth_pick").is_visible(), "Edit profile ethnicity dropdown")
    check(pg.locator("datalist#ppeEthL, #ppe_ethnicity[list]").count()==0, "no leftover datalist")
    opts = pg.locator("#ppe_eth_pick option").all_inner_texts()
    check(opts[0].startswith("Choose") and opts[1:]==WANT, f"Edit profile options {opts}")
    check(pg.locator("#ppe_ethnicity").input_value()=="White", "current value still in the text field")
    def open_edit():
        if pg.evaluate("() => !document.getElementById('ppEdit') || document.getElementById('ppEdit').style.display!=='block'"):
            pg.evaluate("editPerson()")
        pg.wait_for_selector("#ppe_eth_pick", state="visible")
        pg.locator("#ppe_eth_pick").evaluate("e=>e.scrollIntoView({block:'center'})")
    pg.locator("#ppe_eth_pick").evaluate("e=>e.scrollIntoView({block:'center'})")
    pg.select_option("#ppe_eth_pick", label="Jewish"); pg.wait_for_timeout(200)
    check(pg.locator("#ppe_ethnicity").input_value()=="Jewish", "picking Jewish fills the field")
    check(pg.locator("#ppe_eth_pick").input_value()=="", "picker resets to Choose…")
    pg.locator("#ppEdit button:has-text('Save')").evaluate("e=>e.scrollIntoView({block:'center'})")
    pg.click("#ppEdit button:has-text('Save')"); pg.wait_for_timeout(1200)
    row = c.execute("SELECT ethnicity, ethnicity_source FROM people WHERE id=?", (pid,)).fetchone()
    check(tuple(row)==("Jewish","manual"), f"saved Jewish {tuple(row)}")
    # restore original
    open_edit()
    pg.select_option("#ppe_eth_pick", label="White"); pg.wait_for_timeout(200)
    check(pg.locator("#ppe_ethnicity").input_value()=="White", "picking White fills the field")
    pg.locator("#ppEdit button:has-text('Save')").evaluate("e=>e.scrollIntoView({block:'center'})")
    pg.click("#ppEdit button:has-text('Save')"); pg.wait_for_timeout(1200)
    row = c.execute("SELECT ethnicity FROM people WHERE id=?", (pid,)).fetchone()
    check(row[0]=="White", f"restored White {row[0]}")
    # --- Add person (close without saving) ---
    pg.evaluate("newPerson()"); pg.wait_for_timeout(300)
    check(pg.locator("#ppe_eth_pick").count()==1 and pg.locator("#ppe_eth_pick").is_visible(), "Add person ethnicity dropdown")
    opts2 = pg.locator("#ppe_eth_pick option").all_inner_texts()
    check(opts2[1:]==WANT, f"Add person options {opts2}")
    pg.select_option("#ppe_eth_pick", label="Black"); pg.wait_for_timeout(200)
    check(pg.locator("#ppe_ethnicity").input_value()=="Black", "Add person picker fills the field")
    pg.evaluate("closeM()")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n"+"\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
