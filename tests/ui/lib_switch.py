"""Headless check (needs playwright + Chrome; not part of the plain test run): picking a library folder filters the grid
even when an earlier all-libraries list answers late (a slow server, or a scan's periodic reload in flight). The late
answer must not overwrite the folder's list; while the folder's list loads, the count says Loading… instead of the old
total; repeated loads ask /api/tags?lib= once per folder, after the film list. Uses a temp library, no images.
  python tests/ui/lib_switch.py"""
import os, sys, tempfile, threading, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db
from urllib.parse import parse_qs
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "roots", [r"D:\Movies", r"D:\Classic"])
def add(title, path):
    fid = c.execute("INSERT INTO films(title,kind) VALUES(?,?)", (title, "film")).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path))
for i in range(6): add(f"Mainstream {i}", rf"D:\Movies\M{i}\M{i}.mkv")
for i in range(3): add(f"Classic {i}", rf"D:\Classic\C{i}\C{i}.mkv")
c.commit()
DELAY = {"all": 0.0, "lib": 0.0}; LOG = []
def slow(wsgi):  # delay /api/films answers: 'all' = no ?lib=, 'lib' = with ?lib=
    def w(env, start):
        if env.get("PATH_INFO") in ("/api/films", "/api/tags"):
            lib = parse_qs(env.get("QUERY_STRING", "")).get("lib", [""])[0]
            LOG.append((round(time.time(), 2), env["PATH_INFO"], lib))
            if env["PATH_INFO"] == "/api/films": time.sleep(DELAY["lib" if lib else "all"])
        return wsgi(env, start)
    return w
srv = make_server("127.0.0.1", 5095, slow(app.app), threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
errs, fails = [], []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 1400, "height": 1000}); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media") else r.continue_())
    pg.goto("http://127.0.0.1:5095/"); pg.wait_for_timeout(1500)
    pg.evaluate("goHome()"); pg.wait_for_timeout(800)
    check(pg.inner_text("#count") == "9 films", "Home lists all libraries: " + pg.inner_text("#count"))
    # 1) an all-libraries list still loading when the folder is picked answers last: the folder's list must stay
    DELAY["all"] = 2.0
    pg.evaluate("void load()"); pg.wait_for_timeout(200)
    pg.click("#libBtn"); pg.click('#libTree .lr[data-v="D:\\\\Classic"]'); pg.wait_for_timeout(3000)
    titles = pg.evaluate("S.films.map(f=>f.title)")
    check(pg.inner_text("#count") == "3 films" and all(t.startswith("Classic") for t in titles),
          f"late all-libraries answer ignored: {pg.inner_text('#count')} {titles}")
    check("D:\\Classic (3)" in pg.inner_text("#libBtn"), "folder button: " + pg.inner_text("#libBtn"))
    DELAY["all"] = 0.0
    # 2) while a folder's list loads, the old total is not shown as if it were the folder's
    pg.evaluate("pickLib('')"); pg.wait_for_timeout(800); DELAY["lib"] = 1.5; del LOG[:]
    pg.evaluate("pickLib('D:\\\\Movies');void load();void load()"); pg.wait_for_timeout(300)
    check(pg.inner_text("#count") != "9 films", "count while the folder loads: " + pg.inner_text("#count"))
    pg.wait_for_timeout(3000)
    check(pg.inner_text("#count") == "6 films", "Movies folder: " + pg.inner_text("#count"))
    tags = [x for x in LOG if x[1] == "/api/tags" and x[2]]; films = [x for x in LOG if x[1] == "/api/films"]
    check(len(tags) == 1, f"/api/tags?lib= asked once for 3 loads: {len(tags)}")
    check(bool(tags) and bool(films) and tags[0][0] >= films[0][0] + 1.4, "tag counts asked after the film list answered")
    DELAY["lib"] = 0.0
    # 3) back to All libraries
    pg.evaluate("pickLib('')"); pg.wait_for_timeout(1000)
    check(pg.inner_text("#count") == "9 films", "All libraries again: " + pg.inner_text("#count"))
    br.close()
srv.shutdown()
print("page errors:", errs)
sys.exit(1 if errs or fails else 0)
