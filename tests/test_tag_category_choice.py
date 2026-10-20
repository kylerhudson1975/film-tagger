"""Adding a tag with an explicitly chosen category moves an existing same-name tag to that category (names are unique);
AI and CSV imports never recategorise; no category keeps the existing one."""
import os, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, app as A

class TagCategoryChoice(unittest.TestCase):
    def setUp(self):
        c = db.connect()
        mk = lambda t: c.execute("INSERT INTO films(title,added_at) VALUES(?,?)", (t + str(time.time()), time.time())).lastrowid
        self.a, self.b = mk("A"), mk("B")
        db.add_tag(c, self.a, "Rape revenge", "Kink", source="ai"); db.add_tag(c, self.a, "Rape revenge", "Kink")
        c.commit(); self.c = c; self.cl = A.app.test_client()
    def tearDown(self): self.c.close()
    cat = lambda self: self.c.execute("SELECT category FROM tags WHERE name='rape REVENGE'").fetchone()[0]

    def test_user_choice_wins(self):
        self.assertEqual(self.cat(), "Kink")
        self.cl.post("/api/bulk/tags", json={"film_ids": [self.b], "add": [{"name": "Rape revenge", "category": "Genre"}]})
        self.assertEqual(self.cat(), "Genre")
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM tags WHERE name='Rape revenge'").fetchone()[0], 1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE t.name='Rape revenge' AND ft.film_id IN (?,?)", (self.a, self.b)).fetchone()[0], 2)

    def test_no_category_keeps_existing(self):
        self.cl.post("/api/bulk/tags", json={"film_ids": [self.b], "add": [{"name": "Rape revenge", "category": None}]})
        self.assertEqual(self.cat(), "Kink")

    def test_ai_and_import_do_not_recategorise(self):
        db.add_tag(self.c, self.b, "Rape revenge", "Genre", source="ai"); self.assertEqual(self.cat(), "Kink")
        db.add_tag(self.c, self.b, "Rape revenge", "Genre", source="import"); self.assertEqual(self.cat(), "Kink")

if __name__ == "__main__": unittest.main()
