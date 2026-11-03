"""'People without a birth date' window: the review list leaves out scene titles stored as a person (scenecast rule: scene
without a ThePornDB id, cast not hand-edited, not credited anywhere else, no hand-edited / identified person row) and names
no title credits any more; it is uncapped, its count is what it holds and the Dashboard reports the same number
(health.bd_review next to health.no_birthday). The background job skips title-as-name entries. scenecast --clear empties a
cast that is only the title / release name (export then says 'no performers listed', never all-adult). No network.
Run: python tests/test_bd_review_window.py"""
import os, sys, json, tempfile
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, birthdates as bd, scenecast
c = db.connect(); bd.ensure(c)
JUNK = "[Dorcel] Megan Escort Deluxe (2016) ( ) [GhostFreakXX]"
def scene(title, cast, path, tpdb_id=None, edited=None):
    fid = c.execute("INSERT INTO films(title,cast_,kind,primary_genre,tpdb_id,edited_fields) VALUES(?,?,'scene','Hardcore',?,?)",
                    (title, cast, tpdb_id, json.dumps(edited) if edited else None)).lastrowid
    c.execute("INSERT INTO files(film_id,path,size) VALUES(?,?,1)", (fid, path)); return fid
f1 = scene(JUNK, JUNK, r"D:\Porn Scenes\Dorcel\[Dorcel] Megan Escort Deluxe XXX (2016) (1080p HEVC) [GhostFreakXX].mp4")
f2 = scene("AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx", "AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx",
           r"D:\Porn Scenes\Vixen\AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx.mp4")
f3 = scene("Hand Kept Cum Inside Me Scene", "Hand Kept Cum Inside Me Scene", r"D:\Porn Scenes\X\Hand Kept Cum Inside Me Scene.mp4", edited=["cast_"])
f4 = scene("Real", "Real Person, Harmony 6341", r"D:\Porn Scenes\X\Real.mp4", tpdb_id="t1")  # ThePornDB names are people
scene("Victoria Voxxx Solo", "Victoria Voxxx", r"D:\Porn Scenes\X\Victoria Voxxx Solo.mp4", tpdb_id="t2")  # a known performer
amb = json.dumps([{"qid": "Q1", "birthday": "1990-01-01"}, {"qid": "Q2", "birthday": "1991-01-01"}])
for n, st in [(JUNK, "not_found"), ("AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx", "ambiguous"), ("Hand Kept Cum Inside Me Scene", "ambiguous"),
              ("Real Person", "ambiguous"), ("Harmony 6341", "ambiguous"), ("Gone Name", "ambiguous")]:
    c.execute("INSERT INTO birthdate_lookup(name_norm,name,status,detail,at) VALUES(?,?,?,?,1)", (db.fold(n), n, st, amb))
for i in range(520):  # more than the old 500 cap
    n = f"Amb Person {chr(65 + i // 26 % 26)}{chr(65 + i % 26)}x{i}"
    c.execute("INSERT INTO films(title,cast_,primary_genre) VALUES(?,?,'Drama')", (f"F{i}", n))
    c.execute("INSERT INTO birthdate_lookup(name_norm,name,status,detail,at) VALUES(?,?,?,?,1)", (db.fold(n), n, "ambiguous", amb))
c.execute("INSERT INTO smart_queue(film_id,added_at) VALUES(?,1)", (f1,)); c.commit()
cl = app.app.test_client()
r = cl.get("/api/birthdates/review").json
names = {x["name"] for x in r["review"]}
assert JUNK not in names and "AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx" not in names, names
assert "Hand Kept Cum Inside Me Scene" in names  # hand-edited cast: never treated as junk
assert {"Real Person", "Harmony 6341"} <= names  # ThePornDB-matched scene cast stays
assert "Gone Name" not in names and r["hidden"]["not_in_library"] == 1, r["hidden"]
assert r["hidden"]["title_as_name"] == 2 and r["count"] == len(r["review"]) == 523, (r["hidden"], r["count"])
assert r["queued_count"] == 0  # the queued film's only cast entry is a title
d = cl.get("/api/dashboard?force=1").json
assert d["health"]["bd_review"] == r["count"] and d["health"]["no_birthday"] == 0, d["health"]
# background job never looks up title-as-name entries
c.execute("DELETE FROM birthdate_lookup WHERE name_norm IN (?,?)", (db.fold(JUNK), db.fold("AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx"))); c.commit()
q = bd.queue(c); assert JUNK not in q and "AHEGAO Extreme Orgasms Summer Vixen Victoria Voxxx" not in q, q
# scenecast --clear: a cast that is only the title is emptied; unknown leftover words ('Summer Vixen') keep the scene for review
kp = db.known_performers(c)
rows = {x["id"]: x for x in scenecast.scene_rows(c)}
p1 = scenecast.propose(c, rows[f1], kp, clear=True); p2 = scenecast.propose(c, rows[f2], kp, clear=True)
assert p1["clear"] and p1["confident"] and p1["new"] == [], p1
assert not p2["confident"] and not p2.get("clear"), p2
assert not scenecast.propose(c, rows[f1], kp)["confident"]  # only with --clear
res = scenecast.apply_plan(c, [p1, p2])
assert res["fixed"] == [f1], res
assert c.execute("SELECT cast_ FROM films WHERE id=?", (f1,)).fetchone()[0] is None
assert c.execute("SELECT cast_ FROM films WHERE id=?", (f2,)).fetchone()[0].startswith("AHEGAO")
ex = db.export_queue(c, [f1])[0]
assert [m["status"] for m in ex["cast_missing_birthdate"]] == ["no_cast"] and ex["age_check"]["status"] == "missing_birthdate", ex
print("ok")
