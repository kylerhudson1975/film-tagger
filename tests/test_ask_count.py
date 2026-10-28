"""Ask a question: 'how many films did <person> act in tagged <a> or <b> or <c>' (fake AI, temp DB, no network).
Regression for Kyle's 'How many films did Brigitte Lahaie act in that are tagged with oral or oral sex or fellatio?', where
the AI grouped by film (20 rows of 1) and no chart rendered. Degenerate per-film counts are retried with feedback, else
collapsed; the count is checked against a direct query (age rule kept); the result carries the titles for the big-number
tile, the per-year context chart and the list. Run: python tests/test_ask_count.py"""
import os, sys, tempfile, json, re
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, reports as R
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
T = {n: c.execute("INSERT INTO tags(name,category) VALUES(?, 'Content')", (n,)).lastrowid for n in ("Fellatio", "Oral Sex", "Blowjob", "Comedy")}
def film(t, y, tags, cast="Brigitte Test"):
    fid = c.execute("INSERT INTO films(title,year,primary_genre,cast_) VALUES(?,?,'Hardcore',?)", (t, y, cast)).lastrowid
    for n in tags: c.execute("INSERT INTO film_tags(film_id,tag_id,status) VALUES(?,?,'approved')", (fid, T[n]))
    return fid
A = film("A", 1976, ["Fellatio", "Oral Sex"]); B = film("B", 1977, ["Fellatio"]); C_ = film("C", 1977, ["Oral Sex"])
D = film("D", 1980, ["Blowjob"]); film("E", 1979, ["Comedy"]); film("Young", 1971, ["Fellatio"])   # 1971: she was 15 -> never counted
film("Other", 1978, ["Fellatio"], cast="Someone Else")
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Brigitte Test','brigitte test','1955-10-12')")
db.set_setting(c, "ai_url", "http://ai.invalid/v1"); db.set_setting(c, "ai_model", "m"); c.commit()
cl = app.app.test_client()
ask = lambda q: cl.post("/api/reports/ask", json={"question": q}).json
Q = "How many films did Brigitte Test act in that are tagged with oral or oral sex or fellatio?"
assert R.count_question(Q) and not R.count_question("How many films per year did Brigitte Test make?")
assert R.mentioned_tags(c, Q) == [("Oral Sex", ["Oral Sex", "Blowjob", "Fellatio"]), ("Fellatio", ["Fellatio", "Blowjob", "Oral Sex"])]
JOIN = ("FROM films f JOIN film_cast fc ON fc.film_id=f.id JOIN film_tags ft ON ft.film_id=f.id AND ft.status='approved'"
        " JOIN tags t ON t.id=ft.tag_id WHERE fc.person LIKE 'Brigitte Test' AND lower(t.name) IN ({})")
PERFILM = "SELECT f.title AS \"#\", COUNT(DISTINCT f.id) AS films " + JOIN.format("'fellatio','oral sex','blowjob'") + " GROUP BY f.id"
GOOD = "SELECT COUNT(DISTINCT f.id) AS films " + JOIN.format("'fellatio','oral sex','blowjob'")
NARROW = "SELECT COUNT(DISTINCT f.id) AS films " + JOIN.format("'fellatio'")
sent = []
def ai(first, second, summary="Brigitte Test acted in 4 films tagged oral sex or fellatio."):
    def f(url, key, model, prompt, timeout=180, **kw):
        sent.append(prompt)
        if "ONE SQLite SELECT" in prompt: return json.dumps({"reading": "count", "sql": second if "Your previous query" in prompt else first})
        return summary
    return f
def check(r, n=4):
    assert r["rows"] == [[n]] and r["count_ids"] == sorted([A, B, C_, D])[:n] if n == 4 else True, r
    ch = r["chart"]; assert ch["type"] == "stat" and ch["tiles"][0]["value"] == n and ch["ids"] == ",".join(map(str, r["count_ids"])), ch
    assert [x["title"] for x in r["titles"]] == ["A", "B", "C", "D"] and r["titles_total"] == 4
    cx = r["context"]; assert cx["range"] is None and cx["total"] == 4 and [(i["label"], i["values"][0]) for i in cx["items"]] == [(1976, 1), (1977, 2), (1978, 0), (1979, 0), (1980, 1)], cx
    assert cx["items"][1]["ids"] == f"{B},{C_}"
# 1) per-film rows of 1 on both tries -> retried with feedback, then collapsed to one total, checked directly
app.ai_chat = ai(PERFILM, PERFILM); r = ask(Q)
assert r["retried"] and "ONE number" in sent[1] and "COUNT(DISTINCT f.id)" in sent[1], sent[1][-400:]
assert "COUNT(DISTINCT f.id) AS films ... with NO GROUP BY" in sent[0] and "'Oral Sex' means tags.name IN ('Oral Sex', 'Blowjob', 'Fellatio')" in sent[0]
assert r["columns"] == ["films"] and any("added up: 4" in n for n in r["notes"]) and any("direct count of the library: 4" in n for n in r["notes"]), r["notes"]
assert r["answer"] == "Brigitte Test acted in 4 films tagged oral sex or fellatio.", r["answer"]
check(r)
# 2) the retry fixes it: single COUNT(DISTINCT) row; titles come from the same query (rewritten to SELECT DISTINCT)
sent.clear(); app.ai_chat = ai(PERFILM, GOOD); r = ask(Q)
assert r["retried"] and r["columns"] == ["films"] and r["sql"] == GOOD; check(r)
# 3) the AI counts only one of the OR'd tags: the direct query wins and says so
sent.clear(); app.ai_chat = ai(NARROW, NARROW, "Brigitte Test acted in 2 films."); r = ask(Q)
assert r["rows"] == [[4]] and any("AI's query counted 2; a direct count of the library found 4" in n for n in r["notes"]), r
assert r["answer"] == "Brigitte Test is credited in 4 titles tagged Oral Sex or Fellatio.", r["answer"]; check(r)
# 4) 'per year' is a breakdown: rows of 1 are fine there, no retry
sent.clear()
PY = "SELECT f.year AS year, COUNT(DISTINCT f.id) AS films, group_concat(DISTINCT f.id) AS film_ids " + JOIN.format("'fellatio'") + " GROUP BY f.year"
app.ai_chat = ai(PY, PY, "1976 and 1977 have one each."); r = ask("How many films per year did Brigitte Test make tagged fellatio?")
assert not r["retried"] and len(r["rows"]) == 2 and r.get("count_ids") is None and r["chart"]["type"] == "series", r
# 5) a narrower question (a year) is never overridden by the person+tag direct count
sent.clear(); app.ai_chat = ai("SELECT COUNT(DISTINCT f.id) AS films " + JOIN.format("'fellatio','oral sex','blowjob'") + " AND f.year=1977",
                              "x", "Brigitte Test acted in 2 films in 1977."); r = ask("How many films did Brigitte Test act in from 1977 tagged fellatio?")
assert r["rows"] == [[2]] and any("ignoring the question's other conditions" in n for n in r["notes"]), r["notes"]
assert [x["title"] for x in r["titles"]] == ["B", "C"]
print("OK test_ask_count")
