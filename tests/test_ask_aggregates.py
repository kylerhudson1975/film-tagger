"""Ask a question: percentage, ratio and single-row aggregate answers (fake AI, temp DB, no network).
Regression for 'Of the 209 films that Jesus Franco directed... what percent were directed between 1970 and 1990?'
(accent-less name, alias credits, integer-division/NULL percent, '#' row number read as a value, user's stated count).
Run: python tests/test_ask_aggregates.py"""
import os, sys, tempfile, json, re
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, reports as R
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
def film(t, y, d="", pg="Drama", cast=""):
    return c.execute("INSERT INTO films(title,year,director,primary_genre,cast_) VALUES(?,?,?,?,?)", (t, y, d, pg, cast)).lastrowid
for t, y in [("F1", 1965), ("F2", 1972), ("F3", 1975), ("F4", 1980), ("F5", 1995)]: film(t, y, "Jesús Franco", "Horror", "Jesús Franco")
film("P1", 1971, "Clifford Brown", "Hardcore"); film("P2", 1999, "Clifford Brown", "Hardcore")   # pseudonym credits
film("H1", 1977, "Other Dir", "Hardcore"); film("H2", 1978, "Other Dir", "Hardcore")
c.execute("INSERT INTO people(name,name_norm,birthday,aliases) VALUES(?,?,?,?)",
          ("Jesús Franco", "jesus franco", "1930-05-12", json.dumps(["Clifford Brown", "Jess Franco"])))
db.set_setting(c, "ai_url", "http://ai.invalid/v1"); db.set_setting(c, "ai_model", "m"); c.commit()
cl = app.app.test_client()
ask = lambda q: cl.post("/api/reports/ask", json={"question": q}).json

# the stated count is read from the question; the director is found without accents and as a director
assert R.stated_count("Of the 209 films that Jesus Franco directed... what percent were directed between 1970 and 1990?") == 209
assert R.stated_count("What percent of films from 1970 were horror?") is None
assert R.mentioned_people(c, "Of the 209 films that Jesus Franco directed") == [("director", "Jesús Franco")]
assert R.director_film_count(c, "Jesús Franco") == 7

# 1) percentage: first query is Kyle's broken shape (wrong spelling, years in WHERE, integer maths -> NULL percent),
#    it is retried once with the problem fed back; the fixed query uses the stored names incl. the pseudonym.
Q = "Of the 209 films that Jesus Franco directed... what percent were directed between 1970 and 1990?"
sent = []
BAD = ("SELECT ROUND(100*SUM(CASE WHEN f.year BETWEEN 1970 AND 1990 THEN 1 ELSE 0 END)/COUNT(*)) AS percent,"
       " group_concat(f.id) AS film_ids FROM film_directors fd JOIN films f ON f.id=fd.film_id"
       " WHERE fd.director LIKE 'Jesus Franco' AND f.year BETWEEN 1970 AND 1990")
def fake(url, key, model, prompt, timeout=180, **kw):
    sent.append(prompt)
    if "ONE SQLite SELECT" in prompt:
        if "Your previous query" not in prompt: return json.dumps({"reading": "percent", "sql": BAD})
        lst = re.search(r"Match with fd\.director IN \(([^)]*)\)", prompt).group(1)
        cond = "f.year BETWEEN 1970 AND 1990"
        return json.dumps({"reading": "Franco films 1970-1990 as a share of all his films", "sql":
            f"SELECT SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS matching, COUNT(DISTINCT f.id) AS total,"
            f" ROUND(100.0*SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)/NULLIF(COUNT(DISTINCT f.id),0),1) AS percent,"
            f" group_concat(DISTINCT CASE WHEN {cond} THEN f.id END) AS film_ids"
            f" FROM film_directors fd JOIN films f ON f.id=fd.film_id WHERE fd.director IN ({lst})"})
    return "The top result is 1, with 44 films directed between 1970 and 1990."   # the old bad summary
app.ai_chat = fake
r = ask(Q)
assert r["mode"] == "sql" and r["retried"], r
assert "Clifford Brown" in sent[0] and "'Jesús Franco'" in sent[0] and "100.0" in sent[0]       # alias hint + percentage rule
assert "came back empty" in sent[1] or "single row of zeros" in sent[1] or "no rows" in sent[1], sent[1]
assert r["columns"] == ["matching", "total", "percent", "film_ids"] and r["rows"][0][:3] == [4, 7, 57.1], r["rows"]
assert "top result" not in r["answer"].lower() and "57.1%" in r["answer"] and "4" in r["answer"] and "7" in r["answer"], r["answer"]
assert any("You said 209 films; your library has 7 titles directed by Jesús Franco (counting their aliases)" in n for n in r["notes"]), r["notes"]
# a good AI sentence with the real numbers is kept as is
def fake2(url, key, model, prompt, timeout=180, **kw):
    if "ONE SQLite SELECT" in prompt: return fake(url, key, model, prompt + "\nYour previous query", timeout)
    assert "single aggregate result row" in prompt and "#=" not in prompt
    return "57.1% of Jesús Franco's 7 directed films in your library (4) were released 1970–1990."
app.ai_chat = fake2
r = ask(Q)
assert r["answer"] == "57.1% of Jesús Franco's 7 directed films in your library (4) were released 1970–1990.", r["answer"]
# a sentence with wrong numbers is replaced by the plain values
app.ai_chat = lambda u, k, m, p, timeout=180, **kw: fake(u, k, m, p + "\nYour previous query", timeout) if "ONE SQLite SELECT" in p else "21% of his 209 films (44)."
r = ask(Q)
assert r["answer"] == "57.1% (4 of 7) \u2014 Franco films 1970-1990 as a share of all his films.", r["answer"]

# 2) ratio (single row)
def fake_ratio(url, key, model, prompt, timeout=180, **kw):
    if "ONE SQLite SELECT" in prompt:
        assert "ROUND(1.0*a/NULLIF(b,0), 2)" in prompt
        return json.dumps({"reading": "hardcore vs horror", "sql":
            "SELECT SUM(primary_genre='Hardcore') AS hardcore, SUM(primary_genre='Horror') AS horror,"
            " ROUND(1.0*SUM(primary_genre='Hardcore')/NULLIF(SUM(primary_genre='Horror'),0),2) AS ratio FROM films"})
    return "There are 0.8 Hardcore films for every Horror film (4 vs 5)."
app.ai_chat = fake_ratio
r = ask("What is the ratio of Hardcore to Horror films?")
assert r["rows"] == [[4, 5, 0.8]] and r["answer"].startswith("There are 0.8"), r
assert not any("You said" in n for n in r["notes"])

# 3) single-row count: '#'/rank columns are never values; the fallback states the real number
def fake_count(url, key, model, prompt, timeout=180, **kw):
    if "ONE SQLite SELECT" in prompt:
        return json.dumps({"reading": "count", "sql": "SELECT 1 AS rank, COUNT(*) AS films FROM films WHERE director='Other Dir'"})
    return "The top result is 1."
app.ai_chat = fake_count
r = ask("How many films did Other Dir make?")
assert r["rows"] == [[1, 2]] and r["answer"] == "films: 2.", r

# 4) multi-row ranking: a rank column is left out of what the summariser sees
seen = []
def fake_rank(url, key, model, prompt, timeout=180, **kw):
    if "ONE SQLite SELECT" in prompt:
        return json.dumps({"reading": "ranking", "sql": "SELECT ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC) AS rank, director, COUNT(*) AS films"
                           " FROM films GROUP BY director ORDER BY films DESC"})
    seen.append(prompt); return "Jesús Franco leads with 5 films."
app.ai_chat = fake_rank
r = ask("Which director has the most films?")
assert r["answer"].startswith("Jesús Franco") and "rank=" not in seen[0] and "director=Jesús Franco | films=5" in seen[0], seen

# 5) a column that stays NULL after the retry is flagged
app.ai_chat = lambda u, k, m, p, timeout=180, **kw: json.dumps({"reading": "x", "sql": "SELECT NULL AS percent, COUNT(*) AS films FROM films"}) if "ONE SQLite SELECT" in p else "?"
r = ask("What percent of my films are blue?")
assert r["retried"] and any("empty even after a retry" in n for n in r["notes"]), r
print("OK test_ask_aggregates")
# the question's own years may be repeated, the user's (wrong) stated total may not
for good, keep in [("57.1% of Jesús Franco's 7 films (4) came out between 1970 and 1990.", True),
                   ("About 57% of the 7 Franco films were released 1970-1990.", True),
                   ("57.1% of his 209 films were from 1970-1990.", False), ("Most of them.", False)]:
    a = R._summary(Q, ["matching", "total", "percent", "film_ids"], [[4, 7, 57.1, "1,2"]], lambda p: good)
    assert (a == good) == keep, (good, a)
print("OK summary checks")
