"""Find Gallery Cancel stops the backend at once: a running search makes no further forum requests (the 20 s spacing
wait and an in-flight read end immediately), a Cancel clicked right after Download is not lost when the download thread
starts, and after Cancel the download makes no more requests, writes no more files and its process tree is gone. No network."""
import os, sys, tempfile, threading, time, unittest, subprocess
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import findgallery as fg

WRITER = r'''
import os, sys, time
d = sys.argv[1]; i = 0
while True:
    open(os.path.join(d, f"w{i}.jpg"), "wb").write(os.urandom(20000)); i += 1; time.sleep(0.1)
'''

class SearchCancel(unittest.TestCase):
    def setUp(self):
        self.orig = (fg.http, fg.forum_id); fg.forum_id = lambda: "1"; fg._CACHE.clear(); fg.SEARCH_CANCEL.clear()
        self.calls = []
    def tearDown(self): fg.http, fg.forum_id = self.orig; fg.SEARCH_CANCEL.clear()
    def test_cancel_during_slow_read_and_spacing_wait(self):
        def slow(url, data=None, timeout=None): self.calls.append(time.time()); time.sleep(10); return "<html></html>"
        fg.http = slow; fg._STATE["last"] = 0
        box = {}
        def run():
            try:
                for q in ("a", "b", "c"): fg.search(q, block=True)
            except fg.SearchCancelled: box["c"] = True
        t = threading.Thread(target=run, daemon=True); t.start(); time.sleep(0.5)
        t0 = time.time(); fg.cancel_search(); t.join(3)
        self.assertFalse(t.is_alive()); self.assertTrue(box.get("c")); self.assertLess(time.time() - t0, 1.5)
        time.sleep(1); self.assertEqual(len(self.calls), 1)  # no further forum request after Cancel
    def test_cancel_during_spacing_wait(self):
        fg.http = lambda *a, **k: (self.calls.append(1), "<html></html>")[1]; fg._STATE["last"] = time.time()
        t = threading.Thread(target=lambda: self.assertRaises(fg.SearchCancelled, fg.search, "x", None, True), daemon=True)
        t.start(); time.sleep(0.3); t0 = time.time(); fg.cancel_search(); t.join(3)
        self.assertLess(time.time() - t0, 1); self.assertEqual(self.calls, [])

class DownloadCancel(unittest.TestCase):
    def setUp(self):
        fg.DL.clear(); fg.DL["running"] = False; self.dest = os.path.join(tempfile.mkdtemp(), "S - GALLERY")
        self.script = os.path.join(tempfile.mkdtemp(), "w.py"); open(self.script, "w").write(WRITER)
        self.orig = (fg.set_links, fg.expected_count, fg.http)
        self.reqs = []; fg.set_links = lambda url: ([], []); fg.expected_count = lambda url: self.reqs.append(url)
        fg.http = lambda *a, **k: self.reqs.append(a)
    def tearDown(self): fg.set_links, fg.expected_count, fg.http = self.orig
    def popen(self, cmd, **kw):
        self.p = subprocess.Popen([sys.executable, self.script, self.dest]); return self.p
    def test_cancel_before_thread_starts_is_kept(self):
        fg.prepare(); fg.DL.update(running=True); fg.cancel()  # Cancel arrives before the thread runs
        t = threading.Thread(target=fg.run_download, args=("https://vipergirls.to/threads/5", self.dest), kwargs=dict(popen=self.popen, fresh=False), daemon=True)
        t.start(); t.join(10); self.assertFalse(t.is_alive()); self.assertTrue(fg.DL["cancelled"])
        self.assertFalse(hasattr(self, "p") and self.p.poll() is None)
    def test_no_requests_or_writes_after_cancel(self):
        t = threading.Thread(target=fg.run_download, args=("https://vipergirls.to/threads/5", self.dest), kwargs=dict(popen=self.popen), daemon=True)
        t.start(); time.sleep(2.5)
        t0 = time.time(); fg.cancel(); t.join(10)
        self.assertFalse(t.is_alive()); self.assertLess(time.time() - t0, 5)
        self.assertIsNotNone(self.p.poll())  # gallery-dl stand-in is dead
        n = set(os.listdir(self.dest)) if os.path.isdir(self.dest) else set(); r = len(self.reqs)
        time.sleep(1.5)
        self.assertEqual(n, set(os.listdir(self.dest)) if os.path.isdir(self.dest) else set())  # no more file writes
        self.assertEqual(len(self.reqs), r); self.assertEqual(self.reqs, [])  # no forum requests after Cancel

if __name__ == "__main__": unittest.main()
