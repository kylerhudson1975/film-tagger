"""smarttag.py export-queue: lists per film who still lacks a birth date; --no-lookup / --wait parsing; with no app
running and a temp database it never goes online. Run: python tests/test_smarttag_export_bd.py"""
import os, sys, json, tempfile, io, contextlib
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, smarttag, birthdates as bd
c = db.connect(); bd.ensure(c)
fid = c.execute("INSERT INTO films(title,year,cast_) VALUES('Q Film',1970,'Pat Test, Known One')").lastrowid
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Known One','known one','1940-01-01')")
c.execute("INSERT INTO smart_queue VALUES(?,1)", (fid,)); c.commit()
open(os.path.join(d, "port.txt"), "w").write("1")  # nothing listens there: the lookup would run here (offline for temp DBs)
for args in (["--no-lookup"], ["--wait", "5"]):
    out = os.path.join(d, "q.json"); buf = io.StringIO()
    with contextlib.redirect_stdout(buf): assert smarttag.main(["export-queue", out] + args) == 0
    q = json.load(open(out, encoding="utf-8"))
    assert q[0]["cast_missing_birthdate"] == [{"name": "Pat Test", "status": "not_looked_up"}], q
    assert "no birth date for Pat Test [not_looked_up]" in buf.getvalue() and "database: " + db.DB_PATH in buf.getvalue(), buf.getvalue()
print("ok")
