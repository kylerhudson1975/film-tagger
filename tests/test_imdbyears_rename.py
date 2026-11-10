"""IMDb Apply selected and Save rename movie folders, videos, and subtitle sidecars
(including .en.srt / .forced.srt) to the standard Title (Year) name. Temp dirs only."""
import csv, json, os, sys, tempfile, time, unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="ft-imdb-rn-")
os.environ["FILMTAGGER_DB"] = os.path.join(_TMP, "films.db")
os.environ["FILMTAGGER_DATA"] = _TMP
os.environ["FILMTAGGER_NO_IMDB_REFRESH"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import db, imdbyears as iy, app as A


FAKE = [
    ("tt1591095", "movie", "Insidious", "Insidious", 2010),
    ("tt0264616", "movie", "Frailty", "Frailty", 2001),
    ("tt0443701", "movie", "Splice", "Splice", 2009),
]


def touch(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(p, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(data)


class MovieYearRename(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lib-")
        os.environ["FILMTAGGER_DATA"] = _TMP
        iy.build_from_rows(FAKE, dest=iy.db_path(), extra_meta={"source_date": "2026-11-10"})
        self.c = db.connect()
        db.set_setting(self.c, "roots", [self.root])
        self.c.commit()
        self._kick = A.kick_metacritic
        self._gal = A.kick_gallery
        A.kick_metacritic = lambda fid, force=False: False
        A.kick_gallery = lambda fid: None
        self.cl = A.app.test_client()
        self.uid = time.time_ns()

    def tearDown(self):
        A.kick_metacritic = self._kick
        A.kick_gallery = self._gal
        try: self.c.close()
        except Exception: pass

    def movie(self, title, year, sidecars=None, folder=None, filename=None, ed=None, imdb_id=None, **_rest):
        folder = folder or f"{title} ({year})"
        filename = filename or f"{title} ({year}).mkv"
        d = os.path.join(self.root, folder)
        vid = os.path.join(d, filename)
        touch(vid, b"video")
        rels = []
        for name in (sidecars or ()):
            p = os.path.join(d, name)
            touch(p, "1\n00:00:01,000 --> 00:00:02,000\nHi\n" if name.lower().endswith(".srt") else "x")
            rels.append(p)
        ids = {"Insidious": "tt1591095", "Frailty": "tt0264616", "Splice": "tt0443701"}
        if imdb_id is None:
            imdb_id = ids.get(title)
        fid = self.c.execute(
            "INSERT INTO films(title,year,kind,release_date,imdb_id,edited_fields,added_at) VALUES(?,?,?,?,?,?,?)",
            (title, year, None, f"{year}-06-03", imdb_id, ed, time.time()),
        ).lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, vid))
        self.c.commit()
        return fid, vid, rels

    def path(self, fid):
        return self.c.execute("SELECT path FROM files WHERE film_id=?", (fid,)).fetchone()[0]

    def test_preview_lists_folder_video_and_language_tagged_srts(self):
        fid, vid, sides = self.movie(
            "Insidious", 2011,
            sidecars=(
                "Insidious (2011).srt",
                "Insidious (2011).en.srt",
                "Insidious (2011).forced.srt",
                "Insidious (2011).en.forced.srt",
                "Insidious (2011).nfo",
            ),
        )
        r = self.cl.post("/api/imdb-years/preview", json={"film_ids": [fid]}).get_json()
        self.assertTrue(r["ok"])
        item = r["items"][0]
        self.assertTrue(item["ok"])
        self.assertEqual(item["year"], 2011)
        self.assertEqual(item["imdb_year"], 2010)
        kinds = {m["kind"]: [] for m in item["moves"]}
        for m in item["moves"]:
            kinds.setdefault(m["kind"], []).append(m)
            # preview must not move anything
            self.assertTrue(os.path.exists(m["old"]))
        self.assertEqual(len(kinds.get("folder") or []), 1)
        self.assertTrue(kinds["folder"][0]["new"].endswith(os.path.join("Insidious (2010)")))
        self.assertEqual(len(kinds.get("file") or []), 1)
        self.assertTrue(kinds["file"][0]["new"].endswith(os.path.join("Insidious (2010)", "Insidious (2010).mkv")))
        subs = kinds.get("subtitle") or []
        sub_news = sorted(os.path.basename(m["new"]) for m in subs)
        self.assertEqual(sub_news, [
            "Insidious (2010).en.forced.srt",
            "Insidious (2010).en.srt",
            "Insidious (2010).forced.srt",
            "Insidious (2010).srt",
        ])
        nfos = [m for m in item["moves"] if m["kind"] == "sidecar" and m["new"].endswith(".nfo")]
        self.assertEqual(len(nfos), 1)
        # nothing moved
        self.assertTrue(os.path.exists(vid))
        self.assertEqual(self.path(fid), vid)
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (fid,)).fetchone()[0], 2011)

    def test_apply_renames_srts_and_folder_then_updates_year(self):
        fid, vid, _ = self.movie(
            "Frailty", 2002,
            sidecars=("Frailty (2002).srt", "Frailty (2002).en.srt", "Frailty (2002).forced.srt", "Frailty (2002).nfo"),
        )
        # cached MC scores should clear after a successful year change
        self.c.execute("UPDATE films SET mc_slug=?, mc_metascore=?, mc_fetched_at=? WHERE id=?",
                       ("frailty", 73, time.time(), fid))
        self.c.commit()
        r = self.cl.post("/api/imdb-years/apply", json={"film_ids": [fid]}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["applied"], 1)
        self.assertEqual(r["skipped"], 0)
        new = os.path.join(self.root, "Frailty (2001)", "Frailty (2001).mkv")
        self.assertEqual(self.path(fid), new)
        self.assertTrue(os.path.exists(new))
        self.assertFalse(os.path.exists(vid))
        for name in ("Frailty (2001).srt", "Frailty (2001).en.srt", "Frailty (2001).forced.srt", "Frailty (2001).nfo"):
            self.assertTrue(os.path.exists(os.path.join(self.root, "Frailty (2001)", name)), name)
            self.assertFalse(os.path.exists(os.path.join(self.root, "Frailty (2002)", name)), name)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "Frailty (2002)")))
        row = self.c.execute("SELECT year, release_date, imdb_id, mc_slug, mc_metascore FROM films WHERE id=?", (fid,)).fetchone()
        self.assertEqual(row["year"], 2001)
        self.assertEqual(row["release_date"], "2001")
        self.assertEqual(row["imdb_id"], "tt0264616")
        self.assertIsNone(row["mc_slug"])
        log = os.path.join(self.root, "_filmtagger_renames.csv")
        self.assertTrue(os.path.isfile(log))
        rows = list(csv.reader(open(log, encoding="utf-8-sig")))
        self.assertTrue(any(r[0] == vid and r[1] == new for r in rows))
        self.assertTrue(any(r[1].endswith("Frailty (2001).en.srt") for r in rows))

    def test_collision_skips_year_and_leaves_files(self):
        fid, vid, _ = self.movie("Insidious", 2011, sidecars=("Insidious (2011).en.srt",))
        # another film already occupies the target folder
        other = os.path.join(self.root, "Insidious (2010)", "Other.mkv")
        touch(other, b"other")
        oid = self.c.execute("INSERT INTO films(title,year,added_at) VALUES(?,?,?)", ("Other Insidious", 2010, time.time())).lastrowid
        self.c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (oid, other))
        self.c.commit()
        r = self.cl.post("/api/imdb-years/apply", json={"film_ids": [fid]}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["applied"], 0)
        self.assertEqual(r["skipped"], 1)
        self.assertIn("collision", (r["details"][0]["reason"] or "").lower())
        self.assertEqual(self.path(fid), vid)
        self.assertTrue(os.path.exists(vid))
        self.assertTrue(os.path.exists(os.path.join(self.root, "Insidious (2011)", "Insidious (2011).en.srt")))
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (fid,)).fetchone()[0], 2011)

    def test_save_renames_like_apply(self):
        fid, vid, _ = self.movie("Splice", 2010, sidecars=("Splice (2010).en.srt",))
        r = self.cl.put(f"/api/films/{fid}", json={"year": 2009}).get_json()
        new = os.path.join(self.root, "Splice (2009)", "Splice (2009).mkv")
        self.assertEqual(self.path(fid), new)
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(os.path.join(self.root, "Splice (2009)", "Splice (2009).en.srt")))
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (fid,)).fetchone()[0], 2009)

    def test_apply_movie_autofix_renames_to_imdb_year(self):
        fid, vid, _ = self.movie("Splice", 2010, sidecars=("Splice (2010).srt", "Splice (2010).en.srt"))
        A.apply_movie(self.c, fid, {
            "id": 13827, "title": "Splice", "imdb_id": "tt0443701",
            "release_date": "2010-06-03", "credits": {},
        })
        self.c.commit()
        A.after_movie_apply(self.c, fid)
        new = os.path.join(self.root, "Splice (2009)", "Splice (2009).mkv")
        self.assertEqual(self.path(fid), new)
        self.assertTrue(os.path.exists(os.path.join(self.root, "Splice (2009)", "Splice (2009).en.srt")))
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (fid,)).fetchone()[0], 2009)

    def test_preview_is_read_only_when_files_missing(self):
        fid, vid, _ = self.movie("Insidious", 2011)
        os.remove(vid)
        r = self.cl.post("/api/imdb-years/preview", json={"film_ids": [fid]}).get_json()
        self.assertFalse(r["items"][0]["ok"])
        self.assertEqual(self.c.execute("SELECT year FROM films WHERE id=?", (fid,)).fetchone()[0], 2011)


if __name__ == "__main__":
    unittest.main()
