"""db.DB_PATH: external %LOCALAPPDATA%/Film Tagger Data when present, else data/films.db, else films.db;
FILMTAGGER_DB wins. Run: python tests/test_db_default_path.py"""
import os, sys, shutil, subprocess, tempfile
src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = os.path.realpath(tempfile.mkdtemp()); shutil.copy(os.path.join(src, "db.py"), d)
# Isolate from the real Windows library path and any inherited override.
env = {k: v for k, v in os.environ.items() if k not in ("FILMTAGGER_DB", "FILMTAGGER_DATA", "LOCALAPPDATA")}
get = lambda e: os.path.normcase(os.path.realpath(subprocess.check_output(
    [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import db; print(db.DB_PATH)", d],
    cwd=d, env=e, text=True).strip()))
J = lambda *a: os.path.normcase(os.path.join(d, *a))
assert get(env) == J("films.db")
os.makedirs(os.path.join(d, "data")); open(os.path.join(d, "data", "films.db"), "wb").close(); open(os.path.join(d, "films.db"), "wb").close()
assert get(env) == J("data", "films.db"), get(env)
assert get(dict(env, FILMTAGGER_DB=os.path.join(d, "x.db"))) == J("x.db")
# Windows-style: LOCALAPPDATA library wins (and migrates from install\data once).
la = os.path.join(d, "la"); os.makedirs(la)
ext = os.path.join(la, "Film Tagger Data")
# With only legacy data and LOCALAPPDATA set, import migrates then points at the external db.
got = get(dict(env, LOCALAPPDATA=la))
assert got == os.path.normcase(os.path.realpath(os.path.join(ext, "films.db"))), got
assert os.path.isfile(os.path.join(ext, "films.db"))
# FILMTAGGER_DATA override
alt = os.path.join(d, "alt"); os.makedirs(alt); open(os.path.join(alt, "films.db"), "wb").close()
assert get(dict(env, FILMTAGGER_DATA=alt)) == os.path.normcase(os.path.realpath(os.path.join(alt, "films.db")))
print("ok")
