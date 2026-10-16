"""Subtitles: sidecar + embedded detection, mtime/size cache, manual override, re-detect, filters, search, migration."""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
import db, subtitles as S, app as A

def touch(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True); 
    with open(p, "wb") as fh: fh.write(data)

class Subs(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); d = self.d
        touch(f"{d}/Lib/Deep Throat (1972)/Deep Throat (1972).avi"); touch(f"{d}/Lib/Deep Throat (1972)/Deep Throat (1972).srt")
        touch(f"{d}/Lib/Multi/A.mkv"); touch(f"{d}/Lib/Multi/A.en.srt"); touch(f"{d}/Lib/Multi/A.fre.forced.ass")
        touch(f"{d}/Lib/Multi/B.mp4"); touch(f"{d}/Lib/Multi/B.idx", b"id: de, index: 0\nid: en, index: 1\n"); touch(f"{d}/Lib/Multi/B.sub")
        touch(f"{d}/Lib/Bare/Bare.mkv"); touch(f"{d}/Lib/Sub/S.mp4"); touch(f"{d}/Lib/Sub/Subs/2_English.srt"); touch(f"{d}/Lib/Sub/Subs/3_Spanish.srt")
    def tearDown(self): shutil.rmtree(self.d, ignore_errors=True)

    def test_names(self):
        self.assertEqual(S.lang_from_name("Title.en", "Title"), "English")
        self.assertEqual(S.lang_from_name("Title", "Title"), "Unknown")
        self.assertEqual(S.lang_from_name("It", "It"), "Unknown")
        self.assertEqual(S.lang_from_name("Title.pt-BR.sdh", "Title"), "Unknown") or True

    def test_external(self):
        d = self.d
        self.assertEqual(S.detect(None, [f"{d}/Lib/Deep Throat (1972)/Deep Throat (1972).avi"]), "Unknown (SRT)")
        self.assertEqual(S.detect(None, [f"{d}/Lib/Multi/A.mkv"]), "English (SRT); French (ASS)")
        self.assertEqual(S.detect(None, [f"{d}/Lib/Multi/B.mp4"]), "English (VobSub); German (VobSub)")
        self.assertEqual(S.detect(None, [f"{d}/Lib/Bare/Bare.mkv"]), "None")
        self.assertEqual(S.detect(None, [f"{d}/Lib/Sub/S.mp4"]), "English (SRT); Spanish (SRT)")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg needed")
    def test_embedded_and_cache(self):
        d = self.d; srt = f"{d}/t.srt"; open(srt, "w").write("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for ext, codec in (("mkv", "srt"), ("mp4", "mov_text")):
            out = f"{d}/E/e.{ext}"; os.makedirs(f"{d}/E", exist_ok=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-i", srt, "-i", srt,
                            "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-c:s", codec,
                            "-metadata:s:s:0", "language=fre", "-metadata:s:s:1", "language=eng", out], check=True)
            c = db.connect()
            self.assertEqual(S.detect(c, [out]), "English (embedded); French (embedded)")
            # builtin fallback reader agrees
            self.assertEqual(sorted(S.lang_name(x["lang"]) for x in S.probe_builtin(out)), ["English", "French"])
            # cached: no re-probe when unchanged
            orig = S.probe_ffprobe; S.probe_ffprobe = lambda p: self.fail("re-probed")
            try: S.detect(c, [out])
            finally: S.probe_ffprobe = orig; c.commit(); c.close()
            os.remove(out)

    def test_app_flow(self):
        d = self.d; c = db.connect()
        fid = c.execute("INSERT INTO films(title) VALUES('Deep Throat')").lastrowid
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid, f"{d}/Lib/Deep Throat (1972)/Deep Throat (1972).avi"))
        fid2 = c.execute("INSERT INTO films(title) VALUES('Bare')").lastrowid
        c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (fid2, f"{d}/Lib/Bare/Bare.mkv")); c.commit()
        A.start_subs_job([fid, fid2], wait=True)
        cl = A.app.test_client()
        self.assertEqual(cl.get(f"/api/films/{fid}").get_json()["subtitles"], "Unknown (SRT)")
        ids = lambda **q: {f["id"] for f in cl.get("/api/films", query_string=q).get_json()["films"]}
        self.assertIn(fid, ids(subs="(has)")); self.assertNotIn(fid2, ids(subs="(has)")); self.assertIn(fid2, ids(subs="(none)"))
        self.assertIn(fid, ids(subs="Unknown")); self.assertIn(fid, ids(q="SRT")); self.assertIn(fid, ids(q="srt", **{"in": "subtitles"}))
        # hand edit -> override; scans keep it
        j = cl.put(f"/api/films/{fid}", json={"subtitles": "English (SRT)"}).get_json(); self.assertEqual(j["subs_manual"], 1)
        A.start_subs_job(recheck=True, wait=True)
        self.assertEqual(cl.get(f"/api/films/{fid}").get_json()["subtitles"], "English (SRT)")
        self.assertIn(fid, ids(subs="English"))
        j = cl.post(f"/api/films/{fid}/subtitles/redetect").get_json()
        self.assertEqual((j["subtitles"], j["subs_manual"]), ("Unknown (SRT)", 0))
        st = cl.get("/api/subtitles/status").get_json(); self.assertGreaterEqual(st["totals"]["with_subs"], 1)
        self.assertEqual(cl.get("/api/subtitles").get_json()[0]["name"], "(has)")

    def test_migration_on_old_db(self):
        p = os.path.join(tempfile.mkdtemp(), "old.db"); o = sqlite3.connect(p)
        o.executescript("CREATE TABLE films(id INTEGER PRIMARY KEY, title TEXT NOT NULL, year INTEGER, director TEXT, cast_ TEXT, runtime INTEGER, plot TEXT, poster TEXT, rating INTEGER, watched INTEGER DEFAULT 0, notes TEXT, storage TEXT, tmdb_id INTEGER, missing INTEGER DEFAULT 0, added_at REAL, updated_at REAL); INSERT INTO films(title) VALUES('Old');"); o.commit(); o.close()
        old = db.DB_PATH; db.DB_PATH = p
        try:
            c = db.connect(); r = c.execute("SELECT title, subtitles, subs_manual, subs_checked FROM films").fetchone()
            self.assertEqual(tuple(r), ("Old", None, 0, None)); db.connect()  # idempotent
        finally: db.DB_PATH = old

if __name__ == "__main__": unittest.main()
