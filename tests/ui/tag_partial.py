"""Headless check (needs playwright + Chrome; not part of the plain test run): the sidebar tag boxes suggest tags from part
of a name ('object' lists 'Object insertion', tags starting with the text first), the list narrows while typing, and on a
person's 'Credits in my library' list (?pin=) picking the tag keeps the person filter. Temp library, no images.
  python tests/ui/tag_partial.py"""
import os, sys, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\Porn Scenes"])
def add(title, cast, tags):
    fid = c.execute("INSERT INTO films(title,kind,cast_,year,release_date) VALUES(?,?,?,?,?)", (title, "scene", cast, 2020, "2020-05-01")).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, rf"D:\Porn Scenes\Studio\{title}.mp4"))
    for t in tags: db.add_tag(c, fid, t, "Content")
add("Scene A", "Penny Test", ["Object insertion"]); add("Scene B", "Penny Test", ["Solo"])
add("Scene C", "Other Person", ["Sex objects", "Object insertion"]); c.commit()
pid = app.app.test_client().get("/api/person/lookup?name=Penny Test").json["id"]
c.execute("UPDATE people SET birthday='1995-01-01' WHERE id=?", (pid,)); c.commit()
srv = make_server("127.0.0.1", 5096, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
    pg = br.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5096/"); pg.wait_for_timeout(1500)
    pg.evaluate(f"ppGo({pid})"); pg.wait_for_timeout(1200)   # the person page's 'Show their titles in your library'
    titles = lambda: sorted(pg.evaluate("S.films.map(f=>f.title)"))
    check(titles() == ["Scene A", "Scene B"], f"pin list {titles()}")
    pg.click("#f_all"); pg.keyboard.type("object"); pg.wait_for_timeout(900)
    sug = pg.evaluate("[...document.querySelectorAll('#sug_all div span')].map(x=>x.textContent)")
    check(sug[:2] == ["Object insertion", "Sex objects"], f"suggestions {sug}")
    check(titles() == ["Scene A"], f"typing 'object' on Penny's credits: {titles()}")
    pg.keyboard.press("ArrowDown"); pg.keyboard.press("Enter"); pg.wait_for_timeout(900)
    check(pg.input_value("#f_all") == "Object insertion", f"picked {pg.input_value('#f_all')!r}")
    check(pg.evaluate("$('f_pin').value") == str(pid), "person filter kept")
    check(titles() == ["Scene A"], f"after pick {titles()}")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
