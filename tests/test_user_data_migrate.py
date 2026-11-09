"""Library migrate: install\\data is copied into FILMTAGGER_DATA / %LOCALAPPDATA%\\Film Tagger Data without
overwriting, films.db is the commit marker, and custom poster files survive. Run: python tests/test_user_data_migrate.py"""
import os, sys, shutil, tempfile, subprocess
src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
root = os.path.realpath(tempfile.mkdtemp())
code = os.path.join(root, "app"); os.makedirs(code)
shutil.copy(os.path.join(src, "db.py"), code)
legacy = os.path.join(code, "data", "posters", "people")
os.makedirs(legacy)
# pretend library
open(os.path.join(code, "data", "posters", "film-33-test.jpg"), "wb").write(b"POSTER")
open(os.path.join(code, "data", "posters", "people", "manual-1.jpg"), "wb").write(b"PHOTO")
open(os.path.join(code, "data", "port.txt"), "w").write("5050\n")
# films.db last in real migrate; here just create it in legacy
open(os.path.join(code, "data", "films.db"), "wb").write(b"DB")

la = os.path.join(root, "la"); os.makedirs(la)
env = {k: v for k, v in os.environ.items() if k not in ("FILMTAGGER_DB", "FILMTAGGER_DATA", "LOCALAPPDATA")}
env = dict(env, LOCALAPPDATA=la)
out = subprocess.check_output(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, sys.argv[1]); import db; "
     "print(db.DB_PATH); print(db.user_data_dir()); "
     "import os; p=os.path.join(db.user_data_dir(),'posters','film-33-test.jpg'); print(open(p,'rb').read().decode()); "
     "p2=os.path.join(db.user_data_dir(),'posters','people','manual-1.jpg'); print(open(p2,'rb').read().decode())",
     code],
    cwd=code, env=env, text=True).strip().splitlines()
ext = os.path.join(la, "Film Tagger Data")
assert os.path.normcase(os.path.realpath(out[0])) == os.path.normcase(os.path.realpath(os.path.join(ext, "films.db"))), out
assert out[2] == "POSTER" and out[3] == "PHOTO", out
# legacy originals still there (copy, not move)
assert open(os.path.join(code, "data", "posters", "film-33-test.jpg"), "rb").read() == b"POSTER"
# second import does not overwrite a changed external file
open(os.path.join(ext, "posters", "film-33-test.jpg"), "wb").write(b"KEEP")
subprocess.check_output(
    [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import db; print(db.DB_PATH)", code],
    cwd=code, env=env, text=True)
assert open(os.path.join(ext, "posters", "film-33-test.jpg"), "rb").read() == b"KEEP"
# never clobber an existing external library with a different install\data
open(os.path.join(ext, "films.db"), "wb").write(b"KEEPDB")
open(os.path.join(code, "data", "films.db"), "wb").write(b"NEWDB")
# remove marker? films.db exists so migrate is a no-op
subprocess.check_output(
    [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import db; print(open(db.DB_PATH,'rb').read().decode())", code],
    cwd=code, env=env, text=True)
assert open(os.path.join(ext, "films.db"), "rb").read() == b"KEEPDB"
print("ok")
