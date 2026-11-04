"""Headless check (needs playwright + Chrome; not part of the plain test run): a person page's Career timeline fills in by
itself when the ThePornDB scene list finishes loading in the background, with no scrolling or F5, even when another
person's page was opened just before (its refresh timer must not block this one) and the server answers Insights slowly
(a slow, part-loaded answer must never overwrite the complete one). Temp library, fake ThePornDB, text only, no images.
  python tests/ui/timeline_live.py"""
import os, sys, tempfile, threading, time
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import app, db, tpdb
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
c = db.connect(); db.set_setting(c, "tpdb_key", "t"); db.set_setting(c, "scene_roots", ["D:\\S"])
for i, n in enumerate(("Ava Early", "Bea Later"), 1):
    c.execute("INSERT INTO films(id,title,kind,cast_,release_date,year) VALUES(?,?,?,?,?,?)", (i, f"Mine {i}", "scene", n, "2012-03-01", 2012))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, f"D:\\S\\Mine {i}.mp4"))
c.commit()
N, PER = 650, 100; gate = threading.Event()
def tp(path, tok, **k):
    if path == "/performers":
        q = k.get("q") or k.get("parse") or ""
        nm = "Bea Later" if "bea" in str(q).lower() else "Ava Early"
        return {"data": [{"id": nm.split()[0].lower(), "name": nm, "extras": {}}]}
    if path.endswith("/scenes"):
        p = k.get("page", 1); who = path.split("/")[2]
        if p == 6: gate.wait(30)
        ids = range((p - 1) * PER, min(p * PER, N))
        yr = lambda i: 2023 + (i % 2) if i >= 500 else 2013 + i % 10
        return {"data": [{"id": f"{who}{i:04d}", "title": f"Scene {i}", "date": f"{yr(i)}-01-01", "site": {"name": "S"}} for i in ids],
                "meta": {"total": N, "last_page": 7, "current_page": p}}
    pid_ = path.split("/")[-1]
    return {"data": {"id": pid_, "name": "Bea Later" if pid_ == "bea" else "Ava Early", "extras": {}}}
tpdb.call_retry = tp
cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Ava Early", "Bea Later")}
for i in ids.values(): c.execute("UPDATE people SET birthday='1990-01-01' WHERE id=?", (i,))
c.commit()
slow = {"s": 0}
view = app.app.view_functions["person_insights"]
def slow_view(pid):
    r = view(pid)                      # figures worked out now, answer delivered late (busy server / slow network)
    if slow["s"]: time.sleep(slow["s"])
    return r
app.app.view_functions["person_insights"] = slow_view
srv = make_server("127.0.0.1", 5098, app.app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
fails = []
def check(ok, msg):
    if not ok: fails.append(msg)
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox", "--blink-settings=imagesEnabled=false"])
    pg = br.new_page(); errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:5098/"); pg.wait_for_timeout(1500)
    pg.evaluate("localStorage.ppInsOpen='1'")
    pg.evaluate(f"showPerson({ids['Ava Early']})"); pg.wait_for_timeout(800)      # Ava's scenes start loading (held at page 6)
    slow["s"] = 2.5                                                                 # server busy: Insights answers take 2.5 s
    pg.evaluate(f"showPerson({ids['Bea Later']})"); pg.wait_for_timeout(1000)      # open Bea before Ava's refresh timer fires
    check(pg.evaluate("S.page") == f"person:{ids['Bea Later']}", "on Bea's page")
    gate.set()                                                                      # both scene lists finish in the background
    ok = False
    for _ in range(40):
        pg.wait_for_timeout(500)
        h = pg.evaluate("($('ppIns')||{}).innerHTML||''")
        if "Career timeline" in h and "2024:" in h and "2012:" in h and "still loading" not in h: ok = True; break
    check(ok, "Bea's Career timeline did not fill in with every year on its own: " + pg.inner_text("#ppIns")[:300].replace("\n", " | "))
    dd = pg.evaluate(f"PI.d[{ids['Bea Later']}]") or {}
    check(dd.get("total") == N + 1 and not dd.get("loading"), f"insights total {dd.get('total')} loading {dd.get('loading')}")
    check(not pg.evaluate("S.ppData.scenes_loading") and "Loading ThePornDB scenes" not in pg.inner_text("#content"), "Bea's scene list refreshed when loading ended")
    check(pg.evaluate("$('content').scrollTop") == 0, "no scrolling happened")
    check(not errs, f"page errors {errs}")
    br.close()
srv.shutdown()
print("FAIL\n" + "\n".join(fails) if fails else "ok"); sys.exit(1 if fails else 0)
