"""Release date (sort key 'year') defaults to descending when switched to, same as Date added / Last edited.
The ascending/descending toggle still works. Saved searches keep their stored order.
Run: python tests/test_release_sort_default.py"""
import os, re, sys, tempfile, threading, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, ROOT)
import app, db
from werkzeug.serving import make_server

HTML = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()


class ReleaseSortDefaultStatic(unittest.TestCase):
    def test_helper_and_call_sites(self):
        self.assertIn("function sortDefaultOrder(k)", HTML)
        self.assertRegex(HTML, r"function sortDefaultOrder\(k\)\{return \(k=='year'\|\|k=='edited'\|\|k=='added'\)\?'desc':'asc'\}")
        self.assertIn("S.order=sortDefaultOrder(k)", HTML)  # sortBy + onchange
        self.assertIn("S.order=sortDefaultOrder(this.value)", HTML)  # person-header Sort by
        # switching to title still defaults ascending via the helper
        self.assertIn("sortDefaultOrder", HTML)


class ReleaseSortDefaultUI(unittest.TestCase):
    """Playwright check: picking Sort: Release date lands on descending; toggle still flips."""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright not installed")
        c = db.connect()
        db.set_setting(c, "roots", [r"D:\Movies"])
        now = time.time()
        for title, year, rd in (("Old", 1979, "1979-01-01"), ("Mid", 1999, "1999-06-15"), ("New", 2020, "2020-12-01")):
            fid = c.execute("INSERT INTO films(title,year,release_date,added_at) VALUES(?,?,?,?)",
                            (title, year, rd, now)).lastrowid
            c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, rf"D:\Movies\{title}\{title}.mkv"))
        c.commit()
        cls.srv = make_server("127.0.0.1", 5098, app.app, threaded=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        chrome = os.environ.get("CHROME", "/usr/bin/google-chrome")
        cls.br = cls.pw.chromium.launch(executable_path=chrome, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        try: cls.br.close()
        except Exception: pass
        try: cls.pw.stop()
        except Exception: pass
        try: cls.srv.shutdown()
        except Exception: pass

    def test_release_date_defaults_desc(self):
        pg = self.br.new_page(viewport={"width": 1200, "height": 800})
        pg.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media") else r.continue_())
        pg.goto("http://127.0.0.1:5098/")
        pg.wait_for_timeout(800)
        # start on title / asc
        order0 = pg.evaluate("[$('sort').value, S.order]")
        self.assertEqual(order0[0], "title")
        # switch via the Sort dropdown
        pg.select_option("#sort", "year")
        pg.wait_for_timeout(500)
        self.assertEqual(pg.evaluate("[$('sort').value, S.order, $('ord').textContent]"), ["year", "desc", "↓"])
        want = {"Old", "Mid", "New"}
        titles = [x for x in pg.evaluate("S.films.map(f=>f.title)") if x in want]
        self.assertEqual(titles, ["New", "Mid", "Old"], titles)
        # toggle still works
        pg.click("#ord")
        pg.wait_for_timeout(400)
        self.assertEqual(pg.evaluate("S.order"), "asc")
        titles = [x for x in pg.evaluate("S.films.map(f=>f.title)") if x in want]
        self.assertEqual(titles, ["Old", "Mid", "New"], titles)
        # sortBy column path (list view): switch away then back
        pg.select_option("#sort", "title")
        pg.wait_for_timeout(300)
        pg.evaluate("sortBy('year')")
        pg.wait_for_timeout(400)
        self.assertEqual(pg.evaluate("[$('sort').value, S.order]"), ["year", "desc"])
        pg.close()


if __name__ == "__main__":
    unittest.main()
