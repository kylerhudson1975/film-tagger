"""Ask a question: Groq settings, schema covers gender/scenes, built-ins don't swallow rankings.
Temp DB, no network (fake AI). Run: python tests/test_ask_groq.py"""
import os, sys, tempfile, json
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, reports as R

c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)

def add_person(name, gender, birthday):
    c.execute("INSERT INTO people(name,name_norm,gender,birthday) VALUES(?,?,?,?)",
              (name, name.lower(), gender, birthday))

def scene(title, year, cast, rd=None):
    return c.execute(
        "INSERT INTO films(title,year,cast_,kind,release_date) VALUES(?,?,?,'scene',?)",
        (title, year, cast, rd or f"{year}-06-01")).lastrowid

def film(title, year, cast):
    return c.execute("INSERT INTO films(title,year,cast_,kind) VALUES(?,?,?,NULL)",
                     (title, year, cast)).lastrowid

# Three women with different scene counts, two men, plus a film (should not count as a scene)
add_person("Ann Alpha", "Female", "1985-01-15")
add_person("Bea Beta", "Female", "1988-03-02")
add_person("Cara Gamma", "Female", "1990-07-20")
add_person("Dee Delta", "Female", "1992-11-11")
add_person("Eve Epsilon", "Female", "1991-04-04")
add_person("Frank Male", "Male", "1984-02-02")
add_person("Gina NoDate", "Female", None)  # no birthday: age rule excludes her
for i in range(5):
    scene(f"A{i}", 2020, "Ann Alpha")
for i in range(3):
    scene(f"B{i}", 2021, "Bea Beta, Frank Male")
for i in range(2):
    scene(f"C{i}", 2022, "Cara Gamma")
scene("D0", 2023, "Dee Delta")
scene("E0", 2024, "Eve Epsilon")
scene("F0", 2020, "Frank Male")
scene("G0", 2020, "Gina NoDate")  # excluded (no birth date)
film("Feature", 2019, "Ann Alpha")  # film, not a scene
c.commit()

Q = "List the Top 5 Female Porn Stars by Most Scenes"
SQL = (
    "SELECT p.name AS performer, COUNT(DISTINCT fc.film_id) AS scenes, "
    "group_concat(DISTINCT fc.film_id) AS film_ids "
    "FROM film_cast fc JOIN films f ON f.id=fc.film_id AND COALESCE(f.kind,'')='scene' "
    "JOIN people p ON lower(p.name)=lower(fc.person) "
    "WHERE p.gender='Female' GROUP BY lower(p.name) ORDER BY scenes DESC, p.name LIMIT 5"
)

# 1) the question is not claimed by a built-in tag lookup
assert R.ask_direct(c, Q) is None, R.ask_direct(c, Q)
assert not R._likely_person_name(Q)
assert R._likely_person_name("How many films did Brigitte Test act in?")
assert "p.gender" in R.sql_schema(c) and "Female" in R.sql_schema(c) and "kind" in R.sql_schema(c)
schema = R.sql_schema(c)
assert "COALESCE(f.watched,0)=0" in schema and "f.watched=1" in schema
assert "f.rating IS NULL" in schema and "p.favorite=1" in schema
assert "Do not add primary_genre" in schema
assert "How many unwatched films do I have?" in schema
assert "birthday" in schema and "by the time they were 20" in schema
assert "strftime" in schema and "birth_year" in schema

# 2) the documented SQL works on the age-safe read-only connection
rconn, everyone, safe = R._ro_conn(c)
cols, rows, more, dropped = R.run_sql(rconn, SQL, everyone, safe)
rconn.close()
assert cols[:2] == ["performer", "scenes"] and not more, (cols, rows)
assert [row[0] for row in rows] == ["Ann Alpha", "Bea Beta", "Cara Gamma", "Dee Delta", "Eve Epsilon"], rows
assert [row[1] for row in rows] == [5, 3, 2, 1, 1], rows
assert "Frank Male" not in [row[0] for row in rows]
assert "Gina NoDate" not in [row[0] for row in rows]

# 3) no Groq key: settings don't leak a key; test endpoint wants a key; built-ins still work
cl = app.app.test_client()
s = cl.get("/api/settings").json
assert s["ask_ai_key_set"] is False and s["ask_ai_model"] == app.ASK_GROQ_DEFAULT
assert "ask_ai_key" not in s and s["ask_ai_models"][0][0] == "openai/gpt-oss-120b"
t = cl.post("/api/ask/test", json={})
assert t.status_code == 400 and "key" in t.json["error"].lower(), t.json
# blank keep / save / hint / never return the key / clear with "-"
r = cl.put("/api/settings", json={"ask_ai_key": "gsk_test_key_ABCDEFG1234", "ask_ai_model": "openai/gpt-oss-20b"})
assert r.status_code == 200 and r.json["ask_ai_key_set"] and r.json["ask_ai_key_hint"].endswith("1234")
assert r.json["ask_ai_model"] == "openai/gpt-oss-20b" and "gsk_test" not in json.dumps(r.json)
r = cl.put("/api/settings", json={"ask_ai_key": ""})  # blank = keep
assert r.json["ask_ai_key_set"] is True
r = cl.put("/api/settings", json={"ask_ai_key": "-"})
assert r.json["ask_ai_key_set"] is False and not r.json["ask_ai_key_hint"]

# built-in count still works with no Groq key
# (this temp DB has 1 film + 14 scenes; "How many films" excludes scenes)
r = cl.post("/api/reports/ask", json={"question": "How many films do I have?"}).json
assert r.get("direct") and r["rows"][0][0] == 1, r

# 4) with a Groq key and a fake chat, the ranking question is sent to the LLM and the SQL is run
db.set_setting(c, "ask_ai_key", "gsk_fake"); db.set_setting(c, "ask_ai_model", "openai/gpt-oss-120b"); c.commit()
sent = []
def fake(url, key, model, prompt, timeout=180, **kw):
    sent.append((url, model, prompt, timeout))
    if "checking a SQLite query" in prompt:
        return json.dumps({"ok": True})
    if "ONE SQLite SELECT" in prompt:
        assert "p.gender" in prompt and "Female" in prompt
        assert "List the Top 5 Female" in prompt
        assert "Do not add primary_genre" in prompt
        assert "COALESCE(f.watched,0)=0" in prompt
        return json.dumps({"reading": "top 5 female performers by scenes", "sql": SQL, "chart": "hbar"})
    return "Ann Alpha leads with 5 scenes, then Bea Beta (3), Cara Gamma (2), Dee Delta (1) and Eve Epsilon (1)."
app.ai_chat = fake
r = cl.post("/api/reports/ask", json={"question": Q}).json
assert r["mode"] == "sql" and not r.get("direct") and r["rows"][0][0] == "Ann Alpha" and r["rows"][0][1] == 5, r
assert "Ann Alpha" in r["answer"] and r["rows"][0][0] != "Frank Male"
assert sent and sent[0][0] == app.ASK_GROQ_URL and sent[0][1] == "openai/gpt-oss-120b"
# no file paths in what we send
assert not any("path" in (p[2].lower()) and "C:\\\\" in p[2] for p in sent)

assert any("checking a SQLite query" in (p[2] or "") for p in sent)

# 5) verify follow-up: keep SQL when ok or unusable; accept a safe correction; reject a write
ok, sql0, _ = R._parse_verify_reply('{"ok": true}', "SELECT 1")
assert ok and sql0 == "SELECT 1"
ok, sql0, _ = R._parse_verify_reply("not json", "SELECT 1")
assert ok and sql0 == "SELECT 1"
bad = "SELECT COUNT(*) AS films FROM films f WHERE COALESCE(f.kind,'')<>'scene'"
good = "SELECT COUNT(*) AS films FROM films f WHERE COALESCE(f.kind,'')<>'scene' AND COALESCE(f.watched,0)=0"
ok, sql0, reading = R._parse_verify_reply(json.dumps({"ok": False, "sql": good, "reading": "unwatched films"}), bad)
assert not ok and "watched" in sql0 and reading == "unwatched films"
kept, _ = R._verify_sql("How many unwatched films do I have?", bad, lambda p: json.dumps({"ok": False, "sql": "DELETE FROM films"}))
assert kept == bad
fixed, reading = R._verify_sql("How many unwatched films do I have?", bad, lambda p: json.dumps({"ok": False, "sql": good, "reading": "unwatched"}))
assert "watched" in fixed and reading == "unwatched"

# full ask(): first SQL drops unwatched; verify puts it back
c.execute("UPDATE films SET watched=1 WHERE title='Feature'")
c.execute("INSERT INTO films(title,year,cast_,kind,watched) VALUES('Unseen',2018,'Ann Alpha',NULL,0)")
c.commit()
UNWATCHED_Q = "How many unwatched films do I have?"
assert R.ask_direct(c, UNWATCHED_Q) is None
sent.clear()
def fake_unwatched(url, key, model, prompt, timeout=180, **kw):
    sent.append(prompt)
    if "checking a SQLite query" in prompt:
        return json.dumps({"ok": False, "sql": good, "reading": "count of unwatched films"})
    if "ONE SQLite SELECT" in prompt:
        return json.dumps({"reading": "all films", "sql": bad, "chart": "stat"})
    return "You have 1 unwatched film."
app.ai_chat = fake_unwatched
r = cl.post("/api/reports/ask", json={"question": UNWATCHED_Q}).json
assert r["mode"] == "sql" and "watched" in (r.get("sql") or ""), r
assert r["rows"][0][0] == 1, r
assert any("checking a SQLite query" in p for p in sent)

print("OK test_ask_groq")
