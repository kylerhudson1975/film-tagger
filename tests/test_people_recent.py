"""Performers > Recently added (/api/people/recent): cast of the most recently added titles, newest first, one card per
person (deduped, merged people resolved) with the date her latest title was added; cat=porn / mainstream filters by the
Mainstream / Porn rule. Run: python tests/test_people_recent.py"""
import os, sys, time, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "roots", [r"D:\Movies"])
T = time.mktime((2026, 11, 1, 12, 0, 0, 0, 0, -1))
films = [(1, "Old Scene", "scene", "Pia Porn", T - 86400 * 9), (2, "New Scene", "scene", "Pia Porn, Zoe Zest", T),
         (3, "Movie", None, "Max Main, Amy Alias", T - 86400 * 2), (4, "Older Movie", None, "Max Main", T - 86400 * 30)]
for i, t, k, cast, at in films:
    c.execute("INSERT INTO films(id,title,kind,cast_,added_at) VALUES(?,?,?,?,?)", (i, t, k, cast, at))
    c.execute("INSERT INTO files(film_id,path) VALUES(?,?)", (i, (rf"D:\S\{t}.mp4" if k else rf"D:\Movies\{t}.mkv")))
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Pia Porn", "Zoe Zest", "Max Main", "Amy Alias", "Amy Real")}
c.execute("UPDATE people SET merged_into=? WHERE id=?", (ids["Amy Real"], ids["Amy Alias"])); c.commit()
day = lambda at: time.strftime("%Y-%m-%d", time.localtime(at))
r = cl.get("/api/people/recent").json["people"]
assert [(p["name"], p["added"]) for p in r] == [("Pia Porn", day(T)), ("Zoe Zest", day(T)), ("Max Main", day(T - 86400 * 2)), ("Amy Real", day(T - 86400 * 2))], r
assert [p["adult"] for p in r] == [True, True, False, False]
assert [p["name"] for p in cl.get("/api/people/recent?cat=porn").json["people"]] == ["Pia Porn", "Zoe Zest"]
assert [p["name"] for p in cl.get("/api/people/recent?cat=mainstream").json["people"]] == ["Max Main", "Amy Real"]
assert len(cl.get("/api/people/recent?limit=1").json["people"]) == 1
mx = [p["name"] for p in cl.get("/api/people/recent?cat=mix").json["people"]]
assert mx == ["Max Main", "Pia Porn", "Amy Real", "Zoe Zest"], mx   # Mainstream and Porn in turn
print("ok")
