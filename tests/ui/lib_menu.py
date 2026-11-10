"""Headless check (needs playwright + Chrome; not part of the plain test run): reopening the All libraries menu
scrolls the folder list back to the top, clears and focuses the filter, and picking a library starts the grid at the
top. Uses a temp library with enough folders to scroll, no images.
  python tests/ui/lib_menu.py"""
import os, sys, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect()
roots = [r"D:\Movies"] + [rf"D:\Lib{i}" for i in range(30)]
db.set_setting(c, "roots", roots)
def add(title, path):
    fid = c.execute("INSERT INTO films(title,kind) VALUES(?,?)", (title, "film")).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, path))
for i in range(40): add(f"Movie {i:02d}", rf"D:\Movies\M{i:02d}\M{i:02d}.mkv")
for i in range(30): add(f"Lib {i}", rf"D:\Lib{i}\L{i}.mkv")
c.commit()
srv = make_server("127.0.0.1", 5096, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
errs, fails = [], []
def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 1400, "height": 800}); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media") else r.continue_())
    pg.goto("http://127.0.0.1:5096/"); pg.wait_for_timeout(1500)
    pg.evaluate("goHome()"); pg.wait_for_timeout(800)
    # open menu, type a filter, scroll the tree, pick a late folder
    pg.click("#libBtn")
    pg.wait_for_timeout(200)
    check(pg.is_visible("#libPop.open"), "menu opens")
    pg.fill("#libSearch", "Lib2")
    pg.wait_for_timeout(200)
    pg.evaluate("$('libTree').scrollTop=400")
    # close without picking, then reopen: must reset filter, focus, and scroll
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(100)
    pg.click("#libBtn")
    pg.wait_for_timeout(200)
    check(pg.evaluate("document.getElementById('libPop').classList.contains('open')"), "menu reopens")
    check(pg.evaluate("document.getElementById('libSearch').value") == "", "filter cleared")
    check(pg.evaluate("document.activeElement && document.activeElement.id") == "libSearch", "filter focused")
    check(pg.evaluate("document.getElementById('libTree').scrollTop") == 0, "tree scrolled to top after reopen (filter was set): " + str(pg.evaluate("document.getElementById('libTree').scrollTop")))
    # scroll down the full list and pick a folder near the bottom
    st = pg.evaluate("""() => {
      const t = document.getElementById('libTree');
      t.scrollTop = t.scrollHeight;
      return t.scrollTop;
    }""")
    check(st > 0, f"tree is scrollable (scrollTop={st})")
    pg.click('#libTree .lr[data-v="D:\\\\Lib29"]')
    pg.wait_for_timeout(1200)
    check("D:\\Lib29" in pg.inner_text("#libBtn"), "picked Lib29: " + pg.inner_text("#libBtn"))
    # reopen: again at the top, empty filter, focused
    pg.click("#libBtn")
    pg.wait_for_timeout(200)
    check(pg.evaluate("document.getElementById('libSearch').value") == "", "filter still empty on reopen after a pick")
    check(pg.evaluate("document.activeElement && document.activeElement.id") == "libSearch", "filter focused after pick")
    check(pg.evaluate("document.getElementById('libTree').scrollTop") == 0, "tree at top after picking a low folder: " + str(pg.evaluate("document.getElementById('libTree').scrollTop")))
    pg.keyboard.press("Escape")
    # grid: scroll the movie list, pick a library, grid returns to top
    pg.evaluate("pickLib('D:\\\\Movies')")
    pg.wait_for_timeout(1200)
    mid = pg.evaluate("""() => {
      const c = document.getElementById('content');
      c.scrollTop = Math.min(800, Math.max(200, c.scrollHeight - c.clientHeight));
      return c.scrollTop;
    }""")
    check(mid > 0, f"grid was scrolled (scrollTop={mid})")
    pg.evaluate("pickLib('D:\\\\Lib0')")
    pg.wait_for_timeout(1200)
    check(pg.evaluate("document.getElementById('content').scrollTop") == 0, "grid at top after picking a library: " + str(pg.evaluate("document.getElementById('content').scrollTop")))
    br.close()
srv.shutdown()
print("page errors:", errs)
sys.exit(1 if errs or fails else 0)
