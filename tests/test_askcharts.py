"""Ask a question charts: type picked from the result shape (fake AI results), AI 'chart' hint validated, click-through
filters, the per-year context chart with the range shaded, and the ids= list filter. Temp DB, no network.
Run: python tests/test_askcharts.py"""
import os, sys, tempfile, json
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, askcharts as A
P = lambda cols, rows, hint=None, sql="", d=(): A.pick(cols, rows, hint, sql, d)

# --- shapes -> chart types
FR = ("SELECT SUM(CASE WHEN f.year BETWEEN 1970 AND 1990 THEN 1 ELSE 0 END) AS matching, COUNT(DISTINCT f.id) AS total,"
      " ROUND(100.0*SUM(CASE WHEN f.year BETWEEN 1970 AND 1990 THEN 1 ELSE 0 END)/NULLIF(COUNT(DISTINCT f.id),0),1) AS percent,"
      " group_concat(DISTINCT CASE WHEN f.year BETWEEN 1970 AND 1990 THEN f.id END) AS film_ids"
      " FROM film_directors fd JOIN films f ON f.id=fd.film_id WHERE fd.director IN ('Jesús Franco', 'Clifford Brown')")
ch = P(["matching", "total", "percent", "film_ids"], [[50, 55, 90.9, "1,2"]], sql=FR, d=["Jesús Franco", "Clifford Brown"])
assert ch["type"] == "ring" and (ch["part"], ch["total"], ch["pct"]) == (50, 55, 90.9) and ch["label"] == "1970\u20131990", ch
assert ch["total_filter"] == {"person": "Jesús Franco", "prole": "director"}
assert ch["part_filter"] == {"person": "Jesús Franco", "prole": "director", "year_min": 1970, "year_max": 1990} and ch["part_ids"] == "1,2"
ch = P(["films"], [[42]]); assert ch["type"] == "stat" and ch["tiles"] == [{"label": "films", "value": 42, "pct": False}]
ch = P(["rank", "films"], [[1, 7]]); assert ch["type"] == "stat" and [t["label"] for t in ch["tiles"]] == ["films"]   # rank is no value
ch = P(["percent"], [[12.5]]); assert ch["type"] == "ring" and ch["part"] is None
ch = P(["decade", "films", "film_ids"], [[1960, 3, "1"], [1970, 9, "2"], [1980, 4, None]], sql="SELECT ... FROM films WHERE primary_genre LIKE 'Horror' GROUP BY decade")
assert ch["type"] == "series" and ch["mode"] == "bar" and ch["x"] == "decade" and ch["series"] == ["films"]
assert ch["items"][1]["filter"] == {"pgenre": "Horror", "decade": "1970"} and ch["items"][1]["ids"] == "2"
ch = P(["year", "films"], [[1970, 1], [1971, 2]], hint="line"); assert ch["type"] == "series" and ch["mode"] == "line" and ch["hinted"]
assert ch["items"][0]["filter"] == {"year_min": 1970, "year_max": 1970}
ch = P(["director", "films", "film_ids"], [[f"D{i}", 30 - i, str(i)] for i in range(25)])
assert ch["type"] == "hbar" and len(ch["items"]) == 20 and ch["more"] == 5 and ch["items"][0]["filter"] == {"person": "D0", "prole": "director"}
ch = P(["country", "films", "share"], [["Italy", 5, 50.0], ["France", 3, 30.0]])
assert ch["type"] == "grouped" and ch["series"] == ["films", "share"] and ch["pct"] == [False, True] and ch["items"][0]["filter"] == {"country": "Italy"}
ch = P(["genre", "decade", "films"], [["Horror", "1970s", 3], ["Horror", "1980s", 2], ["Drama", "1970s", 1]])
assert ch["type"] == "series"   # a decade column makes it a time series first
ch = P(["genre", "country", "films"], [["Horror", "Italy", 3], ["Horror", "France", 2], ["Drama", "Italy", 1]])
assert ch["type"] == "stacked" and ch["rows_"] == ["Horror", "Drama"] and ch["cols_"] == ["Italy", "France"]
assert ch["cells"][1][1] is None and ch["cells"][0][0]["v"] == 3 and ch["cells"][0][0]["filter"] == {"pgenre": "Horror", "country": "Italy"}
ch = P(["genre", "country", "films"], [["G", f"C{i}", i + 1] for i in range(12)]); assert ch["type"] == "heatmap"
ch = P(["genre", "country", "films"], [["Horror", "Italy", 3], ["Drama", "Italy", 1]], hint="heatmap"); assert ch["type"] == "heatmap" and ch["hinted"]
assert P(["title", "year"], [["A", 1970], ["B", 1971]]) is None or P(["title", "year"], [["A", 1970], ["B", 1971]])["type"] == "series"
assert P(["title", "plot"], [["A", "x"], ["B", "y"]]) is None                       # otherwise: table only
assert P(["films"], [[42]], hint="table") is None                                   # the AI may ask for table only
assert P(["films"], [[42]], hint="heatmap")["type"] == "stat"                       # a hint that doesn't fit is ignored
assert P(["films"], [[42]], hint="<script>")["type"] == "stat"
assert P([], []) is None and P(["films"], []) is None
# context spec: person/genre/country + a year range; never for performers
assert A.context_spec(FR, ["Jesús Franco"]) == {"filter": {"person": "Jesús Franco", "prole": "director"}, "range": [1970, 1990]}
assert A.context_spec("SELECT COUNT(*) FROM films WHERE primary_genre='Horror' AND year >= 1980 AND year < 1990") == {"filter": {"pgenre": "Horror"}, "range": [1980, 1989]}
assert A.context_spec("SELECT COUNT(*) FROM films WHERE year BETWEEN 1970 AND 1979") is None
assert A.context_spec(FR, ["Jesús Franco"], mentions_cast=True) is None
assert A.context_spec("SELECT COUNT(*) FROM films WHERE country LIKE '%Italy%'") is None

# --- end to end through /api/reports/ask with a fake AI
c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
for t, y in [("F1", 1965), ("F2", 1972), ("F3", 1975), ("F4", 1980), ("F5", 1995)]:
    c.execute("INSERT INTO films(title,year,director,primary_genre) VALUES(?,?,?,?)", (t, y, "Jesús Franco", "Horror"))
c.execute("INSERT INTO films(title,year,director,primary_genre) VALUES('P1',1971,'Clifford Brown','Horror')")
c.execute("INSERT INTO films(title,year,director,primary_genre) VALUES('O1',1977,'Other Dir','Drama')")
c.execute("INSERT INTO people(name,name_norm,birthday,aliases) VALUES(?,?,?,?)", ("Jesús Franco", "jesus franco", "1930-05-12", json.dumps(["Clifford Brown"])))
db.set_setting(c, "ai_url", "http://ai.invalid/v1"); db.set_setting(c, "ai_model", "m"); c.commit()
cl = app.app.test_client()
def fake(url, key, model, prompt, timeout=180, **kw):
    if "ONE SQLite SELECT" in prompt:
        assert '"chart": "<optional: ring' in prompt
        lst = prompt.split("Match with fd.director IN (")[1].split(")")[0]
        return json.dumps({"reading": "share", "chart": "ring", "sql": FR.replace("'Jesús Franco', 'Clifford Brown'", lst)})
    return "66.7% of Jesús Franco's 6 directed films (4) were released 1970–1990."
app.ai_chat = fake
r = cl.post("/api/reports/ask", json={"question": "Of the 209 films that Jesus Franco directed, what percentage of them did he direct between 1970 and 1990?"}).json
ch, cx = r["chart"], r["context"]
assert r["rows"][0][:3] == [4, 6, 66.7] and ch["type"] == "ring" and ch["hinted"] and (ch["part"], ch["total"]) == (4, 6), r
assert ch["part_filter"]["year_min"] == 1970 and set(ch["other_ids"].split(",")) == {"1", "5"}      # 1965 + 1995
assert cx["range"] == [1970, 1990] and cx["inside"] == 4 and cx["total"] == 6 and cx["items"][0]["label"] == 1965 and cx["items"][-1]["label"] == 1995
assert "Jesús Franco's directed titles by year" == cx["title"] and len(cx["items"]) == 31
# every clickable count matches the main library list it opens
cnt = lambda f: cl.get("/api/films", query_string=dict(f, limit=1)).json["total"]
assert cnt(ch["part_filter"]) == 4 and cnt(ch["total_filter"]) == 6 and cnt({"ids": ch["other_ids"]}) == 2
for it in cx["items"]: assert cnt(it["filter"]) == it["values"][0], it
assert cl.get("/api/films", query_string={"ids": "1,5"}).json["total"] == 2
print("OK test_askcharts")
# a director bar counting only the credited spelling (not the alias) opens exactly its own titles, not the alias-expanded filter
def fake_rank(url, key, model, prompt, timeout=180, **kw):
    if "ONE SQLite SELECT" in prompt:
        return json.dumps({"reading": "rank", "sql": "SELECT d.director, COUNT(*) AS films, group_concat(DISTINCT d.film_id) AS film_ids FROM film_directors d GROUP BY d.director ORDER BY films DESC"})
    return "Jesús Franco leads with 5 films."
app.ai_chat = fake_rank
r = cl.post("/api/reports/ask", json={"question": "Which directors have the most films?"}).json
it = {x["label"]: x for x in r["chart"]["items"]}
assert it["Jesús Franco"]["values"] == [5] and it["Jesús Franco"]["filter"] is None and cnt({"ids": it["Jesús Franco"]["ids"]}) == 5
assert it["Other Dir"]["filter"] == {"person": "Other Dir", "prole": "director"}
print("OK test_askcharts alias click")
