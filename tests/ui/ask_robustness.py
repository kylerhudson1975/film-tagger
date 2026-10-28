"""Headless check (needs playwright + Chrome; not part of the plain test run): malformed Ask-a-question responses
(null rows/columns, chart/context with missing arrays, null titles) must never hide the answer - each widget falls back.
Also replays responses of the old page code against new answers. Start a demo server first, then:
  B=http://127.0.0.1:5091 python tests/ui/ask_robustness.py tests/ui/bad*.json"""
import json, os, sys
from playwright.sync_api import sync_playwright
B = os.environ.get("B", "http://127.0.0.1:5091"); bad = 0
with sync_playwright() as pw:
    br = pw.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"), args=["--no-sandbox"]); pg = br.new_page()
    cur = {"body": ""}
    def route(r):
        if r.request.resource_type == "image": return r.abort()   # never load images
        if "/api/reports/ask" in r.request.url and r.request.method == "POST": return r.fulfill(status=200, content_type="application/json", body=cur["body"])
        return r.continue_()
    pg.route("**/*", route); pg.goto(B + "/"); pg.wait_for_timeout(1000); pg.evaluate("RP.cur='ask';openReports()"); pg.wait_for_timeout(600)
    for f in sys.argv[1:]:
        j = json.load(open(f)); cur["body"] = json.dumps(j)
        pg.fill("#rpQ", j["question"]); pg.evaluate("rpAsk()"); pg.wait_for_timeout(600)
        out = pg.inner_text("#rpOut"); ok = "Couldn't answer" not in out and (j.get("answer") or "") in out
        bad += not ok; print(("ok  " if ok else "FAIL"), os.path.basename(f))
    br.close()
sys.exit(1 if bad else 0)
