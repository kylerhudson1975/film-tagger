"""Find Gallery download Cancel stops within ~2 s: during a slow gallery-dl run (fake process that keeps writing and
starts a child), during a slow forum page read, and during the URL listing. Half-written files are removed, finished
ones kept. No network: fake processes and a fake slow server."""
import os, sys, tempfile, threading, time, unittest, subprocess, http.server
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import findgallery as fg

FAKE = r'''
import os, sys, time, subprocess
d = sys.argv[1]
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])  # like the venv launcher's real interpreter
for i in range(2):
    open(os.path.join(d, f"img{i}.jpg"), "wb").write(os.urandom(30000 + i))
open(os.path.join(d, "img2.jpg.part"), "wb").write(b"x" * 5000)
time.sleep(120)
'''

class SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(15); self.send_response(200); self.end_headers(); self.wfile.write(b"<html></html>")
    def log_message(self, *a): pass

class Cancel(unittest.TestCase):
    def setUp(self):
        fg.DL.clear(); fg.DL["running"] = False; self.dest = os.path.join(tempfile.mkdtemp(), "S - GALLERY")
        self.script = os.path.join(tempfile.mkdtemp(), "fake_gdl.py"); open(self.script, "w").write(FAKE)
        self.orig = (fg.set_links, fg.expected_count, fg.http)
    def tearDown(self): fg.set_links, fg.expected_count, fg.http = self.orig
    def start(self, popen=None):
        t = threading.Thread(target=fg.run_download, args=("https://vipergirls.to/threads/5", self.dest), kwargs=dict(popen=popen), daemon=True)
        t.start(); return t
    def cancel_and_time(self, t):
        t0 = time.time(); fg.cancel(); t.join(10); return time.time() - t0

    def test_cancel_slow_download(self):
        fg.set_links = lambda url: ([], []); fg.expected_count = lambda url: None
        procs = []
        def popen(cmd, **kw):
            p = subprocess.Popen([sys.executable, self.script, self.dest], stdin=subprocess.DEVNULL); procs.append(p); return p
        t = self.start(popen)
        for _ in range(100):
            if os.path.exists(os.path.join(self.dest, "img2.jpg.part")): break
            time.sleep(0.1)
        dt = self.cancel_and_time(t)
        self.assertLess(dt, 2.5, dt); self.assertFalse(fg.DL["running"]); self.assertTrue(fg.DL["cancelled"])
        self.assertEqual(fg.DL["files"], 2); self.assertEqual(fg.DL["partial_removed"], 1)
        self.assertEqual(sorted(os.listdir(self.dest)), ["img0.jpg", "img1.jpg"]); self.assertIsNotNone(procs[0].poll())

    def test_cancel_during_slow_page_read(self):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler); threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/"
        fg.set_links = lambda u: (fg.http(url), ([], []))[1]
        started = []
        t = self.start(lambda *a, **k: started.append(1) or (_ for _ in ()).throw(AssertionError("gallery-dl started after cancel")))
        time.sleep(0.5); dt = self.cancel_and_time(t)
        self.assertLess(dt, 2.5, dt); self.assertFalse(fg.DL["running"]); self.assertEqual(started, []); self.assertTrue(fg.DL["cancelled"])
        srv.shutdown()

    def test_list_urls_cancel(self):
        fg.CANCEL.clear(); fg.DL["cancel"] = False
        real = subprocess.Popen
        def slow_popen(cmd, **kw): return real([sys.executable, "-c", "import time; time.sleep(60)"] if "gallery_dl" in cmd else cmd, **kw)
        subprocess.Popen = slow_popen
        try:
            threading.Timer(0.5, fg.cancel).start(); t0 = time.time(); r = fg.list_urls("https://x")
        finally: subprocess.Popen = real
        self.assertEqual(r, []); self.assertLess(time.time() - t0, 2.5)

if __name__ == "__main__": unittest.main()
