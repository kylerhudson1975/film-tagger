"""Reports: whitelisted specs, tag synonyms, adult scope/share ranking, performers (known birth dates, adults only),
and Ask a question (AI picks a spec; never SQL; no AI -> helpful 400). Temp DB, no network.
Run: python tests/test_reports.py"""
import os, sys, tempfile, json
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, reports as R
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
def film(t, y, d="", co="", pg="", tags=(), kind=None, st=None, cast="", rd=None):
    i = c.execute("INSERT INTO films(title,year,director,country,primary_genre,kind,studio,cast_,release_date) VALUES(?,?,?,?,?,?,?,?,?)",
                  (t, y, d, co, pg, kind, st, cast, rd)).lastrowid
    for n in tags:
        c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES(?,?)", (n, "Content"))
        tid = c.execute("SELECT id FROM tags WHERE name=?", (n,)).fetchone()[0]
        c.execute("INSERT INTO film_tags(film_id,tag_id) VALUES(?,?)", (i, tid))
    return i
film("A", 1974, "Dir One", "United States of America", "Hardcore", ["Blowjob"])
film("B", 1975, "Dir One", "United States of America, Italy", "Hardcore", ["Fellatio", "Exploitation"])
film("C", 1976, "Dir One", "Italy", "Hardcore")
film("D", 1977, "Dir Two", "France", "Hardcore", ["Oral Sex"])
film("E", 1978, "Dir Two", "France", "Drama", ["Exploitation"])
film("F", 1985, "Dir One", "Italy", "Hardcore", ["Blowjob"])
film("S1", 2020, kind="scene", st="Studio X", tags=["Blowjob"], cast="Grown Up, Unknown Person", rd="2020-05-01")
film("S2", 2003, kind="scene", st="Studio X", cast="Grown Up", rd="2003-01-01")    # before 18th birthday -> excluded
film("S3", 2021, kind="scene", st="Studio Y", cast="Grown Up")
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Grown Up','grown up','1985-06-01')")
c.execute("INSERT INTO people(name,name_norm) VALUES('Unknown Person','unknown person')")
c.commit()
cl = app.app.test_client()
run = lambda **s: cl.post("/api/reports/run", json={"spec": s}).json
# country + tag filter; multi-country counts for each
r = run(report="country", tag="Exploitation")
assert [(x["k"], x["n"]) for x in r["rows"]] == [("France", 1), ("Italy", 1), ("United States of America", 1)] and r["base"] == 2
# directors, 1970s adult, Blowjob with synonyms -> Dir One 2 of 3, Dir Two 1 of 1
r = run(report="directors", tag="blowjob", decade="1970s", scope="adult")
assert [(x["k"], x["n"], x["tot"]) for x in r["rows"]] == [("Dir One", 2, 3), ("Dir Two", 1, 1)], r["rows"]
assert any("synonyms" in n for n in r["notes"]) and "1970s adult films" in r["reading"]
r = run(report="directors", tag="Blowjob", decade="1970s", scope="adult", rank="share")
assert r["rows"][0]["k"] == "Dir Two" and r["rows"][0]["pct"] == 100.0
# synonyms editable; removing the group stops the merge
assert cl.put("/api/reports/synonyms", json={"synonyms": [["Blowjob", "Fellatio"]]}).json["synonyms"] == [["Blowjob", "Fellatio"]]
assert [(x["k"], x["n"]) for x in run(report="directors", tag="Blowjob", decade="1970s", scope="adult")["rows"]] == [("Dir One", 2)]
assert cl.put("/api/reports/synonyms", json={"synonyms": "nope"}).status_code == 400
# tag over decades
r = run(report="tag_decades", tag="Exploitation"); d = {x["k"]: (x["n"], x["tot"]) for x in r["rows"]}
assert d["1970s"] == (2, 5) and d["1980s"] == (0, 1)
assert [(x["k"], x["n"]) for x in run(report="genres")["rows"]][0] == ("Hardcore", 5)
assert [(x["k"], x["n"]) for x in run(report="studios")["rows"]] == [("Studio X", 2), ("Studio Y", 1)]
# performers: known birth date only, adults only (S2 released before 18th birthday excluded), unknown omitted
r = run(report="performers", tag="Blowjob")
assert [(x["k"], x["n"]) for x in r["rows"]] == [("Grown Up", 2)], r["rows"]
assert any("never applied to performers" in n for n in r["notes"])
# whitelist: unknown report/tag/genre reported, no SQL possible
s, probs = R.clean_spec(c, {"report": "x; DROP TABLE films", "tag": "Nope'--", "genre": "Zzz", "decade": "70s", "limit": 10**6})
assert s["report"] == "country" and "tag" not in s and "genre" not in s and s["decade"] == "1970s" and s["limit"] == 100 and len(probs) == 3
assert c.execute("SELECT COUNT(*) FROM films").fetchone()[0] == 9
# film list
ids = run(report="studios")["rows"][0]["ids"]
assert [f["title"] for f in cl.post("/api/reports/films", json={"ids": ids}).json["films"]] == ["S2", "S1"]
# ask: no AI configured -> helpful 400
r = cl.post("/api/reports/ask", json={"question": "Which country made the most exploitation films?"})
assert r.status_code == 400 and "No AI service" in r.json["error"]
# ask with AI (mocked): only the question + vocabulary is sent, reply parsed into a spec
db.set_setting(c, "ai_url", "http://ai.invalid/v1"); db.set_setting(c, "ai_model", "m"); c.commit()
sent = {}
def fake(url, key, model, prompt, timeout=180):
    sent["p"] = prompt
    return '```json\n{"report":"directors","tag":"Blowjob","decade":"1970s","scope":"adult","sql":"DROP TABLE films"}\n```'
app.ai_chat = fake
r = cl.post("/api/reports/ask", json={"question": "Which 1970s porn director made the most films tagged Blowjob?"}).json
assert r["rows"][0]["k"] == "Dir One" and "sql" not in r["spec"] and "sql" not in r["ai_spec"]
assert "Question: Which 1970s porn director" in sent["p"] and '"Blowjob"' in sent["p"] and "S1" not in sent["p"]
app.ai_chat = lambda *a, **k: "sorry, no idea"
assert cl.post("/api/reports/ask", json={"question": "hm?"}).status_code == 502
assert cl.get("/api/reports/options").json["ai_configured"] is True
print("OK test_reports")
