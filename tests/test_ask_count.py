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
assert r["answer"] == "Brigitte Test is credited in 4 titles tagged Oral Sex or Fellatio (counting the synonyms Blowjob).", r["answer"]; check(r)
# 4) 'per year' is a breakdown: rows of 1 are fine there, no retry
sent.clear()
PY = "SELECT f.year AS year, COUNT(DISTINCT f.id) AS films, group_concat(DISTINCT f.id) AS film_ids " + JOIN.format("'fellatio'") + " GROUP BY f.year"
app.ai_chat = ai(PY, PY, "1976 and 1977 have one each."); r = ask("How many films per year did Brigitte Test make tagged fellatio?")
assert not r["retried"] and len(r["rows"]) == 2 and r.get("count_ids") is None and r["chart"]["type"] == "series", r
# 5) a narrower question (a year) is never overridden by the person+tag direct count
sent.clear(); app.ai_chat = ai("SELECT COUNT(DISTINCT f.id) AS films " + JOIN.format("'fellatio','oral sex','blowjob'") + " AND f.year=1977",
                              "x", "Brigitte Test acted in 2 films in 1977."); r = ask("How many films did Brigitte Test act in from 1977 tagged fellatio?")
assert r["rows"] == [[2]] and not any("direct count" in n for n in r["notes"]), r["notes"]
assert [x["title"] for x in r["titles"]] == ["B", "C"]
# 6) percentage with film_tags JOINed into the total (untagged films dropped): the direct count fixes part/total/percent
film("Untagged", 1981, []); c.commit()
JPCT = ("SELECT SUM(CASE WHEN lower(t.name) IN ('oral sex','fellatio','blowjob') THEN 1 ELSE 0 END) AS matching, COUNT(DISTINCT f.id) AS total,"
        " ROUND(100.0*SUM(CASE WHEN lower(t.name) IN ('oral sex','fellatio','blowjob') THEN 1 ELSE 0 END)/NULLIF(COUNT(DISTINCT f.id),0),1) AS percent"
        " FROM film_cast fc JOIN films f ON f.id=fc.film_id JOIN film_tags ft ON ft.film_id=f.id AND ft.status='approved' JOIN tags t ON t.id=ft.tag_id"
        " WHERE fc.person LIKE 'Brigitte Test'")
sent.clear(); app.ai_chat = ai(JPCT, JPCT, "x"); r = ask("What percentage of Brigitte Test films are tagged oral sex?")
assert "EXISTS (SELECT 1 FROM film_tags" in sent[0] and r["rows"][0][:3] == [4, 6, 66.7], r["rows"]   # A-D of A-E + Untagged
assert any("a direct count of the library found 4 of 6" in n for n in r["notes"]) and r["chart"]["type"] == "ring", r
assert r["answer"] == "66.7% of Brigitte Test's 6 titles (4) are tagged Oral Sex.", r["answer"]
# 7) a correct EXISTS percentage is confirmed, not replaced
OK = ("SELECT SUM(CASE WHEN EXISTS (SELECT 1 FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=f.id AND ft.status='approved'"
      " AND lower(t.name) IN ('oral sex','fellatio','blowjob')) THEN 1 ELSE 0 END) AS matching, COUNT(DISTINCT f.id) AS total,"
      " ROUND(100.0*SUM(CASE WHEN EXISTS (SELECT 1 FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=f.id AND ft.status='approved'"
      " AND lower(t.name) IN ('oral sex','fellatio','blowjob')) THEN 1 ELSE 0 END)/NULLIF(COUNT(DISTINCT f.id),0),1) AS percent"
      " FROM film_cast fc JOIN films f ON f.id=fc.film_id WHERE fc.person LIKE 'Brigitte Test'")
app.ai_chat = ai(OK, OK, "66.7% of Brigitte Test's 6 films (4) are tagged oral sex."); r = ask("What percentage of Brigitte Test films are tagged oral sex?")
assert r["rows"][0][:3] == [4, 6, 66.7] and any("Checked against a direct count of the library: 4 of 6" in n for n in r["notes"]) and r["answer"].startswith("66.7%"), r
# 8) a tag the library doesn't have: no person-only 'direct check' passes for it
app.ai_chat = ai(GOOD.replace("'fellatio','oral sex','blowjob'", "'zzz'"), "x", "Brigitte Test acted in 0 films."); r = ask("How many films did Brigitte Test act in tagged zzzunknown?")
assert not any("direct count" in n for n in r["notes"]), r["notes"]
# 9) year-like tags ('1977', '1970s') are never read as tags; no tag check without the word 'tag'
c.execute("INSERT INTO tags(name,category) VALUES('1977','Themes')"); c.execute("INSERT INTO tags(name,category) VALUES('1970s','Themes')"); c.commit()
assert R.mentioned_tags(c, "How many films did Brigitte Test make in 1977 and the 1970s?") == []
app.ai_chat = ai("SELECT COUNT(DISTINCT f.id) AS films FROM film_cast fc JOIN films f ON f.id=fc.film_id WHERE fc.person LIKE 'Brigitte Test' AND f.year=1977", "x",
                 "Brigitte Test made 2 films in 1977."); r = ask("How many films did Brigitte Test make in 1977?")
assert r["rows"] == [[2]] and r["answer"] == "Brigitte Test made 2 films in 1977." and not any("direct" in n for n in r["notes"]), r
# 10) several rows: a sentence whose numbers aren't in the rows (or that calls a year a decade) is replaced
YQ = "SELECT f.year AS decade, COUNT(DISTINCT f.id) AS films FROM film_cast fc JOIN films f ON f.id=fc.film_id WHERE fc.person LIKE 'Brigitte Test' GROUP BY f.year ORDER BY films DESC"
app.ai_chat = ai(YQ, YQ, "She made the most films in the 1977 decade with 57 films."); r = ask("Which year did Brigitte Test make the most films?")
assert "57" not in r["answer"] and r["answer"].startswith("1977: 2 films"), r["answer"]
# 11) Kyle's 'Of all the film that X was in, how many are tagged ...?' answered as a share: the count is shown (24-style)
SH = OK.replace("SELECT SUM(", "SELECT COUNT(DISTINCT f.id) AS films, SUM(", 1)
app.ai_chat = ai(SH, SH, "66.7% of her films."); r = ask("Of all the film that Brigitte Test was in, how many are tagged with oral or oral sex or fellatio?")
assert r["rows"] == [[4]] and r["chart"]["type"] == "stat" and len(r["titles"]) == 4 and r["context"], r
assert any("asks how many" in n for n in r["notes"]) and any("direct count of the library: 4" in n for n in r["notes"]) and "4" in r["answer"], r
# 12) 'kind IS NULL OR kind!='scene' AND ...' without parentheses is repaired before it runs
BADOR = "SELECT COUNT(DISTINCT f.id) AS films FROM films f WHERE f.kind IS NULL OR f.kind!='scene' AND f.year=1977"
app.ai_chat = ai(BADOR, BADOR, "2 films."); r = ask("How many Classic Porn films are from 1977?")
assert r["sql"].count("(f.kind IS NULL OR f.kind!='scene') AND") == 1 and r["rows"] == [[2]], r   # B and C (unparenthesised it would count every film)
# every response names the page build (a tab opened before an update offers a reload)
assert cl.get("/api/reports/ask").headers.get("X-FT-Build") or cl.get("/").headers.get("X-FT-Build")
print("OK test_ask_count")
