"""Headless check (needs playwright + Chrome; not part of the plain test run): the ☆/⭐ star on a person page adds and
removes a Favorite, People → ⭐ Favorites lists them A-Z by first name then last name with library scene counts, the
empty state explains how to add one, and starred people get a small ⭐ in the People cards. Temp library, text only, no images.
  python tests/ui/favorites.py"""
import os, sys, tempfile, threading
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\Porn Scenes"])
def add(title, cast):
    fid = c.execute("INSERT INTO films(title,kind,cast_,year,release_date) VALUES(?,?,?,?,?)", (title, "scene", cast, 2020, "2020-05-01")).lastrowid
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, rf"D:\Porn Scenes\Studio\{title}.mp4"))
add("Scene A", "Zoe Adams, Anna Zed"); add("Scene B", "Anna Zed"); add("Scene C", "Anna Baker"); c.commit()
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Zoe Adams", "Anna Zed", "Anna Baker")}
for i in ids.values(): c.execute("UPDATE people SET birthday='1995-01-01' WHERE id=?", (i,))
c.commit()
srv = make_server("127.0.0.1", 5097, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5097/"); pg.wait_for_timeout(1500)
    pg.evaluate("setIn('people');localStorage.ppFav='1';S.ppFav=true;load()"); pg.wait_for_timeout(900)
    txt = lambda: pg.inner_text("#content")
    check("No favorites yet" in txt() and "☆ star" in txt(), f"empty state: {txt()[:200]!r}")
    for n in ("Zoe Adams", "Anna Zed", "Anna Baker"):  # star each one from their page
        pg.evaluate(f"showPerson({ids[n]})"); pg.wait_for_timeout(900)
        check(pg.inner_text("#ppFavBtn") == "☆", f"{n}: star before {pg.inner_text('#ppFavBtn')!r}")
        pg.click("#ppFavBtn"); pg.wait_for_timeout(500)
        check(pg.inner_text("#ppFavBtn") == "⭐", f"{n}: star after {pg.inner_text('#ppFavBtn')!r}")
    pg.evaluate(f"showPerson({ids['Anna Zed']})"); pg.wait_for_timeout(900)
    check(pg.inner_text("#ppFavBtn") == "⭐", "star kept after reopening")
    pg.evaluate("S.page=null;setIn('people');S.ppFav=true;load()"); pg.wait_for_timeout(900)
    names = pg.evaluate("[...document.querySelectorAll('#ppFav .pcard b')].map(x=>x.textContent)")
    check(names == ["Anna Baker", "Anna Zed", "Zoe Adams"], f"favorites order {names}")
    check("2 scenes in library" in txt() and "1 scene in library" in txt(), f"counts: {txt()[:300]!r}")
    check("⭐ Favorites" in txt() and "(3)" in txt(), "header")
    pg.click("#ppFav .pcard >> text=Zoe Adams"); pg.wait_for_timeout(900)
    check(pg.evaluate("S.page") == f"person:{ids['Zoe Adams']}", f"click opens profile: {pg.evaluate('S.page')}")
    pg.click("#ppFavBtn"); pg.wait_for_timeout(500)   # un-star Zoe
    check(pg.inner_text("#ppFavBtn") == "☆", "un-starred")
    pg.evaluate("S.page=null;setIn('people');S.ppFav=true;load()"); pg.wait_for_timeout(900)
    names = pg.evaluate("[...document.querySelectorAll('#ppFav .pcard b')].map(x=>x.textContent)")
    check(names == ["Anna Baker", "Anna Zed"], f"after un-star {names}")
    # People search: small star next to starred names only
    pg.evaluate("S.ppFav=false;localStorage.ppFav='0';S.ppBuilt=false;localStorage.ppBuilt='0'"); pg.fill("#q", "anna"); pg.evaluate("load()"); pg.wait_for_timeout(900)
    cards = pg.evaluate("[...document.querySelectorAll('.pcard')].map(x=>x.querySelector('b').textContent+(x.querySelector('.pfs')?' *':''))")
    check(sorted(cards) == ["Anna Baker *", "Anna Zed *"], f"people cards {cards}")
    pg.fill("#q", "zoe"); pg.evaluate("load()"); pg.wait_for_timeout(900)
    cards = pg.evaluate("[...document.querySelectorAll('.pcard')].map(x=>x.querySelector('b').textContent+(x.querySelector('.pfs')?' *':''))")
    check(cards == ["Zoe Adams"], f"zoe card {cards}")
    check(pg.locator("#ppFavTg").count() == 1, "Favorites link in the People header")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
