"""Headless check (needs playwright + Chrome; not part of the plain test run) of the Dashboard 'People without a birth date'
window: tile and window title show both counts and they equal the lists; no scene titles in the review list; links use
the accent colour; the window moves by its header, resizes from a corner, remembers its geometry and resets on
double-click. Images are never loaded. Start a server first, then:  B=http://127.0.0.1:5093 python tests/ui/bd_window.py"""
import os, re
from playwright.sync_api import sync_playwright
B = os.environ.get("B", "http://127.0.0.1:5093"); errs = []
num = lambda t: int(re.sub(r"\D", "", t) or 0)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 1500, "height": 1000})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: r.abort() if r.request.resource_type == "image" else r.continue_())
    pg.goto(B + "/"); pg.evaluate("localStorage.removeItem('win.nobday')")
    pg.evaluate("showDash({force:true})"); pg.wait_for_selector(".dsh-chip", timeout=60000)
    chip = pg.locator(".dsh-chip", has_text="People without birth dates")
    ct = chip.inner_text(); print("tile:", " | ".join(ct.split("\n")))
    m = re.search(r"([\d,]+) person pages? · ([\d,]+) to review", ct); assert m, ct
    n_pages, n_rev = num(m.group(1)), num(m.group(2))
    chip.click(); pg.wait_for_selector("#bdRev h4", timeout=60000)
    title = pg.inner_text("#bdwCounts"); print("title:", title)
    m2 = re.search(r"([\d,]+) person pages? without a birth date · ([\d,]+) names? to review", title); assert m2, title
    rev_h = pg.inner_text("#bdRev h4"); pages_h = pg.inner_text(".bdw-b > h4"); print("lists:", pages_h, "/", rev_h.split(" more")[0])
    rev_items = pg.locator("#bdRev .dsh-pl > div").count(); page_items = pg.locator(".bdw-b > .dsh-pl > div").count()
    assert num(m2.group(2)) == rev_items == num(re.search(r"\(([\d,]+)\)", rev_h).group(1)), (title, rev_h, rev_items)
    assert num(m2.group(1)) == n_pages and page_items == min(n_pages, 500), (n_pages, page_items)
    assert n_rev == rev_items, (n_rev, rev_items)  # tile = list
    names = pg.eval_on_selector_all("#bdRev .dsh-pl > div > b", "els=>els.map(e=>e.textContent)")
    bad = [x for x in names if re.search(r"\[|\]|GhostFreak|AHEGAO|\(\d{4}\)|-scene-\d", x)]; assert not bad, bad[:10]
    # links readable: the accent colour, not the browser's default blue / purple
    cols = set(pg.eval_on_selector_all(".bdw a", "els=>els.map(e=>getComputedStyle(e).color)")); print("link colours:", cols)
    assert cols == {"rgb(91, 156, 255)"}, cols
    for t in ("check IAFD", "none of these"):
        assert pg.locator(".bdw a", has_text=t).count() > 0, t
    # move by the header
    box0 = pg.locator("#mbox").bounding_box(); hd = pg.locator(".bdw .fts-head h2").bounding_box()
    pg.mouse.move(hd["x"] + 20, hd["y"] + 8); pg.mouse.down(); pg.mouse.move(hd["x"] - 100, hd["y"] + 68, steps=6); pg.mouse.up()
    box1 = pg.locator("#mbox").bounding_box(); print("moved", box0, "->", box1)
    assert abs(box1["x"] - (box0["x"] - 120)) < 3 and abs(box1["y"] - (box0["y"] + 60)) < 3
    # resize from the bottom-right corner
    se = pg.locator(".fw-h[data-d=se]").bounding_box()
    pg.mouse.move(se["x"] + 8, se["y"] + 8); pg.mouse.down(); pg.mouse.move(se["x"] - 140, se["y"] - 90, steps=6); pg.mouse.up()
    box2 = pg.locator("#mbox").bounding_box(); print("resized ->", box2)
    assert abs(box2["width"] - (box1["width"] - 148)) < 4 and abs(box2["height"] - (box1["height"] - 98)) < 4
    sh = pg.evaluate("(()=>{const b=document.querySelector('.bdw-b');return [b.scrollHeight,b.clientHeight]})()"); assert sh[0] > sh[1], sh  # body scrolls
    # remembered after a reload
    pg.goto(B + "/"); pg.evaluate("showDash({force:true})"); pg.wait_for_selector(".dsh-chip", timeout=60000)
    pg.locator(".dsh-chip", has_text="People without birth dates").click(); pg.wait_for_selector("#bdRev h4", timeout=60000)
    box3 = pg.locator("#mbox").bounding_box(); print("reopened ->", box3)
    assert all(abs(box3[k] - box2[k]) < 2 for k in ("x", "y", "width", "height")), (box2, box3)
    pg.dblclick(".bdw .fts-head h2"); box4 = pg.locator("#mbox").bounding_box(); print("reset ->", box4)
    assert box4["width"] == min(920, round(1500 * .95)), box4
    # Settings window still uses its own default size
    pg.keyboard.press("Escape")
    assert not errs, errs
    print("UI OK")
    br.close()
