"""Headless check (needs playwright + Chrome; not part of the plain test run) of the studio pages and the Studios list:
no script errors, studio names open studio pages, list sort/filter/folder, 'Show these titles' equals the in-library count,
Back returns. Images are never loaded. Start a demo server first (any library with studios), then:
  B=http://127.0.0.1:5091 STUDIO="Nordisk Demo" python tests/ui/studio_pages.py"""
import os, sys, re
from playwright.sync_api import sync_playwright
B = os.environ.get("B", "http://127.0.0.1:5091"); NAME = os.environ.get("STUDIO", "Nordisk Demo"); SHOT = os.environ.get("SHOT")
errs = []
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"])
    pg = br.new_page(viewport={"width": 1400, "height": 1000})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.route("**/*", lambda r: r.abort() if r.request.resource_type == "image" else r.continue_())
    pg.goto(B + "/"); pg.wait_for_timeout(1500)
    # dashboard Top studios card: All studios link
    pg.evaluate("openStudios()"); pg.wait_for_timeout(1200)
    assert pg.locator(".st-list").count() == 1, "studios list not shown"
    assert pg.eval_on_selector("#f_in", "e=>e.value") == "studios"
    n_rows = pg.locator(".st-list tbody tr").count(); print("studios rows", n_rows)
    pg.evaluate("slSort('name')"); first = pg.inner_text(".st-list tbody tr:first-child td:first-child"); print("first by name:", first.strip())
    pg.evaluate("slSort('n')"); pg.wait_for_timeout(200)
    pg.fill("#q", NAME[:5]); pg.evaluate("load()"); pg.wait_for_timeout(800)
    assert pg.locator(".st-list tbody tr").count() >= 1
    pg.locator(".st-list tbody tr").first.click(); pg.wait_for_timeout(1500)
    assert pg.evaluate("String(S.page).startsWith('studio:')"), "studio page not open"
    txt = pg.inner_text("#content"); assert "Insights" in txt and "Career timeline" in txt, txt[:500]
    if SHOT: pg.screenshot(path=SHOT, full_page=False)
    nlib = pg.evaluate("ST.d.in_library"); name = pg.evaluate("ST.d.name"); print("page:", name, "in library", nlib)
    pg.evaluate("stSet('stTl','all')"); pg.evaluate("stSet('stShow','all')"); pg.evaluate("stSet('stChart','line')"); pg.wait_for_timeout(200)
    pg.evaluate("stSet('stTl','lib')"); pg.evaluate("stSet('stShow','lib')"); pg.evaluate("stSet('stChart','bar')")
    # Show these titles = same count in the library list
    pg.evaluate(f"stGo(ST.d.name)"); pg.wait_for_timeout(1500)
    total = pg.evaluate("S.total"); print("list total", total); assert total == nlib, (total, nlib)
    assert "studio page" in pg.inner_text("#personHdr")
    pg.go_back(); pg.wait_for_timeout(1500)
    assert pg.evaluate("String(S.page).startsWith('studio:')"), "back did not return to the studio page"
    pg.go_back(); pg.wait_for_timeout(1500)
    assert pg.locator(".st-list").count() == 1, "back did not return to the list"
    # film details: studio name opens the studio page
    pg.evaluate("setIn('');$('q').value='';load()"); pg.wait_for_timeout(1200)
    fid = pg.evaluate("(S.films.find(f=>f.studio)||{}).id")
    if fid:
        pg.evaluate(f"openFilm({fid})"); pg.wait_for_timeout(1000)
        pg.locator("#mbox a.clink[onclick*=showStudio]").first.click(); pg.wait_for_timeout(1500)
        assert pg.evaluate("String(S.page).startsWith('studio:')"), "film details studio link"
    # country edit round trip (hand edit), then back to automatic
    pg.evaluate("showStudio(%r)" % name); pg.wait_for_timeout(1200)
    pg.once("dialog", lambda d: d.accept("Norway")); pg.evaluate("stEditCountry()"); pg.wait_for_timeout(1200)
    assert pg.evaluate("ST.d.country") == "Norway" and pg.evaluate("ST.d.country_src") == "hand"
    pg.evaluate("stEditCountry(true)"); pg.wait_for_timeout(1200)
    assert pg.evaluate("ST.d.country_src") != "hand"
    br.close()
print("page errors:", errs)
sys.exit(1 if errs else 0)
