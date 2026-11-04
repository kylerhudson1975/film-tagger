"""Headless check (playwright + Chrome; images off; Make stills endpoints mocked, no video touched): the Make stills
popup offers 10 / 20 (default) / 40, adds Add to existing / Replace only when the folder has images, posts the
choice, and the done toast shows the count saved with an Open folder button.  python tests/ui/stills_prompt.py"""
import os, sys, tempfile, threading, json, re
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
srv = make_server("127.0.0.1", 5107, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
state = dict(existing=0, posts=[], opened=0)
def info(r): r.fulfill(status=200, content_type="application/json", body=json.dumps(dict(existing=state["existing"], dest="X - GALLERY", counts=[10, 20, 40], default=20)))
def post(r):
    if r.request.method != "POST": return r.continue_()
    b = json.loads(r.request.post_data or "{}"); state["posts"].append(b)
    r.fulfill(status=200, content_type="application/json", body=json.dumps(dict(ok=True, dest="X - GALLERY", count=b.get("count"), backup=".stills-backup-1" if b.get("mode") == "replace" else None)))
def status(r): r.fulfill(status=200, content_type="application/json", body=json.dumps(dict(running=False, error=None, done=10, saved=state["posts"][-1]["count"] if state["posts"] else 0, total=20, dest="X - GALLERY")))
def opn(r): state["opened"] += 1; r.fulfill(status=200, content_type="application/json", body='{"ok":true}')
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(viewport={"width": 1400, "height": 900}); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route(re.compile(r".*/api/films/77/stills/info$"), info); pg.route(re.compile(r".*/api/films/77/stills$"), post)
    pg.route(re.compile(r".*/api/stills/status$"), status); pg.route(re.compile(r".*/api/films/77/gallery/open$"), opn)
    pg.goto("http://127.0.0.1:5107/"); pg.wait_for_timeout(1500)
    pg.evaluate("makeStills(77)"); pg.wait_for_timeout(500)
    btns = pg.evaluate("[...document.querySelectorAll('#stCount button')].map(b=>b.textContent+(b.classList.contains('on')?'*':''))")
    check(btns == ["10", "20*", "40"], f"count choices {btns}"); check(pg.locator("#stMode").count() == 0, "no Add/Replace for an empty folder")
    pg.click("#stPrompt button:has-text('Cancel')"); pg.wait_for_timeout(200); check(pg.locator("#stPrompt").count() == 0 and not state["posts"], "Cancel closes, nothing posted")
    pg.evaluate("makeStills(77)"); pg.wait_for_timeout(500); pg.click("#stGo"); pg.wait_for_timeout(2500)
    check(state["posts"][-1:] == [{"count": 20}], f"default post {state['posts']}")
    check("20 stills saved" in pg.inner_text("#toast") and pg.locator("#toast #stOpen").count() == 1, f"done toast {pg.inner_text('#toast')!r}")
    pg.click("#stOpen"); pg.wait_for_timeout(400); check(state["opened"] == 1, "Open folder button")
    state["existing"] = 12; pg.evaluate("makeStills(77)"); pg.wait_for_timeout(500)
    modes = pg.evaluate("[...document.querySelectorAll('#stMode button')].map(b=>b.textContent+(b.classList.contains('on')?'*':''))")
    check(modes == ["Add to existing*", "Replace"] and "12 images" in pg.inner_text("#stPrompt"), f"add/replace {modes}")
    pg.click("#stCount button[data-n='40']"); pg.click("#stMode button[data-m=replace]"); pg.click("#stGo"); pg.wait_for_timeout(2500)
    check(state["posts"][-1] == {"count": 40, "mode": "replace"}, f"replace post {state['posts'][-1]}")
    check("40 stills saved" in pg.inner_text("#toast"), "toast after replace")
    pg.evaluate("makeStills(77)"); pg.wait_for_timeout(500); pg.click("#stCount button[data-n='10']"); pg.click("#stGo"); pg.wait_for_timeout(2000)
    check(state["posts"][-1] == {"count": 10, "mode": "add"}, f"add post {state['posts'][-1]}")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
