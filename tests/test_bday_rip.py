"""Performers rows: Happy Birthday! (/api/people/birthdays: today's birthdays with the age turning, else the next 7 days;
dead people left out; Feb 29 on Feb 28 in non-leap years) and In memoriam (/api/people/memoriam: most recent death first,
born–died · age); library people only; Mainstream / Porn filter; Death date is a protected, validated hand edit.
Run: python tests/test_bday_rip.py"""
import os, sys, tempfile, datetime as _dt
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db
_c0 = db.connect(); db.set_setting(_c0, "gender_backfill_done", 1); db.set_setting(_c0, "death_backfill_done", 1); _c0.commit()
import app, people
c = db.connect(); db.set_setting(c, "scene_roots", [r"D:\S"]); db.set_setting(c, "roots", [r"D:\Movies"])
c.execute("INSERT INTO films(id,title,kind,cast_) VALUES(1,'Sc','scene','Pia Porn, Leap Lee')"); c.execute("INSERT INTO files(film_id,path) VALUES(1,'D:\\S\\Sc.mp4')")
c.execute("INSERT INTO films(id,title,cast_) VALUES(2,'Mv','Max Main, Dee Dead, Old Gone, Soon Sam')"); c.execute("INSERT INTO files(film_id,path) VALUES(2,'D:\\Movies\\Mv.mkv')")
c.commit(); cl = app.app.test_client()
ids = {n: cl.get("/api/person/lookup?name=" + n).json["id"] for n in ("Pia Porn", "Leap Lee", "Max Main", "Dee Dead", "Old Gone", "Soon Sam", "Not Inlib")}
class FakeDate(_dt.date):
    @classmethod
    def today(cls): return cls(2027, 2, 28)
class FakeDT: date = FakeDate
people.datetime = FakeDT
S = lambda n, bd, dd=None: c.execute("UPDATE people SET birthday=?, deathday=? WHERE id=?", (bd, dd, ids[n]))
S("Pia Porn", "1986-02-28"); S("Leap Lee", "1988-02-29"); S("Max Main", "1970-02-28"); S("Dee Dead", "1943-02-28", "2010-06-01")
S("Old Gone", "1950-12-31", "2020-01-15"); S("Soon Sam", "1990-03-03"); S("Not Inlib", "1960-02-28", "2001-02-02"); c.commit()
b = cl.get("/api/people/birthdays").json
assert b["mode"] == "today" and [(p["name"], p["turns"]) for p in b["people"]] == [("Leap Lee", 39), ("Max Main", 57), ("Pia Porn", 41)], b
assert [p["name"] for p in cl.get("/api/people/birthdays?cat=porn").json["people"]] == ["Leap Lee", "Pia Porn"]
assert [p["name"] for p in cl.get("/api/people/birthdays?cat=mainstream").json["people"]] == ["Max Main"]
FakeDate.today = classmethod(lambda cls: cls(2027, 3, 1))  # nobody today: the next 7 days
w = cl.get("/api/people/birthdays").json
assert w["mode"] == "week" and [(p["name"], p["date"], p["days"], p["turns"]) for p in w["people"]] == [("Soon Sam", "2027-03-03", 2, 37)], w
m = cl.get("/api/people/memoriam").json["people"]
assert [(p["name"], p["born"], p["died"], p["age"]) for p in m] == [("Old Gone", 1950, 2020, 69), ("Dee Dead", 1943, 2010, 67)], m
assert cl.get("/api/people/memoriam?cat=porn").json["people"] == []
# Death date hand edit: validated, protected from the add-only fill
assert cl.post(f"/api/person/{ids['Max Main']}/edit", json={"deathday": "2024-1-5"}).json["ok"]
assert c.execute("SELECT deathday, edited_fields FROM people WHERE id=?", (ids["Max Main"],)).fetchone()[:] == ("2024-01-05", '["deathday"]')
cl.post(f"/api/person/{ids['Soon Sam']}/edit", json={"deathday": "soon"})
assert c.execute("SELECT deathday FROM people WHERE id=?", (ids["Soon Sam"],)).fetchone()[0] is None
assert cl.get(f"/api/people/{ids['Dee Dead']}/summary").json["deathday"] == "2010-06-01"
print("ok")
