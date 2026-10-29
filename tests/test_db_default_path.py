"""db.DB_PATH: data/films.db when it exists (installed layout), else films.db next to the code; FILMTAGGER_DB wins.
Run: python tests/test_db_default_path.py"""
import os, sys, shutil, subprocess, tempfile
src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = os.path.realpath(tempfile.mkdtemp()); shutil.copy(os.path.join(src, "db.py"), d)
env = {k: v for k, v in os.environ.items() if k != "FILMTAGGER_DB"}
get = lambda e: os.path.normcase(os.path.realpath(subprocess.check_output([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import db; print(db.DB_PATH)", d], cwd=d, env=e, text=True).strip()))
J = lambda *a: os.path.normcase(os.path.join(d, *a))
assert get(env) == J("films.db")
os.makedirs(os.path.join(d, "data")); open(os.path.join(d, "data", "films.db"), "wb").close(); open(os.path.join(d, "films.db"), "wb").close()
assert get(env) == J("data", "films.db"), get(env)
assert get(dict(env, FILMTAGGER_DB=os.path.join(d, "x.db"))) == J("x.db")
print("ok")
