"""Headless check (needs playwright + Chrome; not part of the plain test run) of Upgrade file on the film page: button,
compare step (resolution / size / duration, move offer), upgrade, subtitle rename prompt, success note, no script
errors. Runs its own server on a temp library with tiny dummy MP4 headers; the Recycle Bin is faked.
  python tests/ui/upgrade_file.py"""
import os, sys, struct, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db, upgrade
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright

def box(t, payload): return struct.pack(">I", 8 + len(payload)) + t + payload
def mp4(path, w, h, seconds, pad=0):
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">II", 1000, int(seconds * 1000)) + bytes(80))
    tkhd = box(b"tkhd", bytes(76) + struct.pack(">II", w << 16, h << 16))
    data = box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", mvhd + box(b"trak", tkhd + box(b"mdia", box(b"hdlr", bytes(8) + b"vide" + bytes(12))))) + box(b"free", bytes(pad))
    os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "wb").write(data); return path
root = tempfile.mkdtemp(); outside = tempfile.mkdtemp()
c = db.connect(); db.set_setting(c, "roots", [root]); c.commit()
old = mp4(os.path.join(root, "Film (1999)", "Film (1999) 720p.mp4"), 1280, 720, 5400)
sub = os.path.join(root, "Film (1999)", "Film (1999) 720p.en.srt"); open(sub, "w").write("1\n")
db.scan(c, [root]); fid = c.execute("SELECT film_id FROM files").fetchone()[0]
new = mp4(os.path.join(outside, "Film (1999) 1080p.mp4"), 1920, 1080, 5400, pad=5000)
binned = []; upgrade.recycle_problem = lambda p: None; upgrade.recycle = lambda p: (binned.append(p), os.remove(p))[0]
srv = make_server("127.0.0.1", 5094, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
errs = []
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 1400, "height": 1000}); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5094/"); pg.wait_for_timeout(1500)
    pg.evaluate(f"openFilm({fid})"); pg.wait_for_timeout(1000)
    pg.click("text=⬆ Upgrade file"); pg.wait_for_timeout(300)
    assert "Current file" in pg.inner_text("#ufOv")
    pg.click("#ufNext"); pg.wait_for_timeout(300); assert "Choose the new video file" in pg.inner_text("#ufMsg")
    pg.fill("#ufPath", new); pg.click("#ufNext"); pg.wait_for_timeout(1500)
    t = pg.inner_text("#ufOv"); print(t)
    assert "720p" in t and "1080p" in t and "1:30:00" in t and "Move it into the old file's folder" in t and "Recycle Bin" in t
    assert pg.is_checked("#ufMove") and os.path.exists(old) and not binned
    if os.environ.get("SHOT"): pg.screenshot(path=os.environ["SHOT"])
    pg.click("#ufGo"); pg.wait_for_timeout(1500)
    t = pg.inner_text("#ufOv"); print(t); assert "Upgraded 720p → 1080p" in t and "Rename them" in t
    pg.click("#ufSubs"); pg.wait_for_timeout(1200)
    toast = pg.inner_text("#toast"); print("toast:", toast); assert "Upgraded 720p → 1080p" in toast and "renamed 1 subtitle" in toast
    target = os.path.join(root, "Film (1999)", "Film (1999) 1080p.mp4")
    assert pg.input_value("#e_paths") == target and pg.input_value("#e_resolution") == "1080p"
    assert binned == [old] and os.path.exists(target) and os.path.exists(os.path.join(root, "Film (1999)", "Film (1999) 1080p.en.srt"))
    # unsaved edits: Upgrade file asks to save first
    pg.fill("#e_title", "Changed"); msgs = []
    pg.once("dialog", lambda dl: (msgs.append(dl.message), dl.accept())); pg.click("text=⬆ Upgrade file"); pg.wait_for_timeout(300)
    assert msgs and "Save or cancel" in msgs[0]
    br.close()
srv.shutdown()
print("page errors:", errs)
sys.exit(1 if errs else 0)
