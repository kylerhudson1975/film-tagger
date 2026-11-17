"""Ask a question: built-in parameterized queries (no AI). Temp DB, no network.
Run: python tests/test_ask_direct.py"""
import os, sys, tempfile, time
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, people, reports as R

c = db.connect(); c.executescript(people.PEOPLE_SCHEMA); people.migrate(c)
now = time.time()

def film(title, year, director="", genre="", rating=None, studio="", kind=None, added=None, cast=""):
    return c.execute(
        "INSERT INTO films(title,year,director,primary_genre,rating,studio,kind,added_at,cast_) VALUES(?,?,?,?,?,?,?,?,?)",
        (title, year, director, genre, rating, studio, kind, added, cast),
    ).lastrowid

film("H1", 1982, "Jane Director", "Horror", 8, "Studio A", added=now - 100)
film("H2", 1985, "Jane Director", "Horror", 6, "Studio A", added=now - 50)
film("H3", 1991, "Other Dir", "Horror", 4, "Studio B", added=now - 20)
film("D1", 1975, "Other Dir", "Drama", 9, "Studio B", added=now - 10)
film("D2", 1978, "", "Drama", None, "", added=now - 5)          # blank director, unrated
film("D3", 2001, "none", "Drama", 7, "n/a")                      # placeholder names ignored
film("S1", 1984, "Jane Director", "Horror", 5, "Studio A", kind="scene", added=now)
c.execute("INSERT INTO people(name,name_norm,birthday) VALUES('Actor One','actor one','1960-01-01')")
c.execute("UPDATE films SET cast_='Actor One' WHERE title IN ('H1','H2','H3','D1')")
c.commit()

# no AI configured: built-in questions still work
cl = app.app.test_client()
def ask(q):
    r = cl.post("/api/reports/ask", json={"question": q})
    assert r.status_code == 200, (q, r.status_code, r.get_json())
    return r.get_json()

r = ask("How many films from the 1980s do I have?")
assert r["direct"] and r["rows"][0][0] == 2 and "2 films" in r["answer"] and "1980s" in r["answer"], r
assert "tagged" not in r["answer"].lower()
# a tag literally named '1980s' must not change that count
c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES('1980s','Content')")
tid = c.execute("SELECT id FROM tags WHERE name='1980s'").fetchone()[0]
c.execute("INSERT INTO film_tags(film_id,tag_id) VALUES(?,?)", (c.execute("SELECT id FROM films WHERE title='H1'").fetchone()[0], tid))
c.commit()
r = ask("How many films from the 1980s do I have?")
assert r["rows"][0][0] == 2 and "tagged" not in r["answer"].lower(), r

r = ask("Which director has the most films in my library?")
assert r["direct"] and r["rows"][0][0] == "Jane Director" and r["rows"][0][1] == 2, r
assert "Jane Director" in r["answer"] and "none" not in r["answer"].lower()
assert "tagged" not in r["answer"].lower()
c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES('Director','Content')")
c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES('Studio','Content')")
c.commit()
r = ask("Which director has the most films in my library?")
assert r["rows"][0][0] == "Jane Director" and r["rows"][0][1] == 2 and "tagged" not in r["answer"].lower(), r

r = ask("What is my average rating for horror films?")
assert r["direct"] and r["rows"][0][1] == 3 and r["rows"][0][0] == 6.0, r  # (8+6+4)/3, scene excluded
assert "6" in r["answer"]

r = ask("How many titles are unrated?")
assert r["direct"] and r["rows"][0][0] == 1 and "1 unrated" in r["answer"], r

r = ask("How many films do I have?")
assert r["direct"] and r["rows"][0][0] == 6, r  # not the scene

r = ask("How many scenes do I have?")
assert r["direct"] and r["rows"][0][0] == 1, r

r = ask("Which studio has the most titles?")
assert r["direct"] and r["rows"][0][0] == "Studio A", r
assert "n/a" not in r["answer"].lower()

r = ask("Recently added films")
assert r["direct"] and r["rows"][0][0] == "D2", r

r = ask("What are my highest-rated films?")
assert r["direct"] and r["rows"][0][0] == "D1" and r["rows"][0][2] == 9, r

r = ask("How many films from the 1950s do I have?")
assert r["direct"] and r["rows"][0][0] == 0 and "no films" in r["answer"].lower(), r

# leftover questions get an honest fallback when no AI is configured
r = ask("What is the meaning of life in my library?")
assert r.get("answer_from_error") and "couldn't answer that" in r["answer"].lower()
assert "1980s" in r["answer"]

# person questions are left for the existing path (not claimed as a built-in miss)
assert R.ask_direct(c, "How many films did Jane Director make?") is None
assert R.ask_direct(c, "What is the ratio of Horror to Drama films?") is None

# tag / name lookup: "Do I have any X scenes?"
c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES('BGGG','Content')")
tid = c.execute("SELECT id FROM tags WHERE name='BGGG'").fetchone()[0]
# H1 and H2 are 1980s horror films; S1 is a horror scene
for (title,) in c.execute("SELECT title FROM films WHERE title IN ('H1','S1')"):
    fid = c.execute("SELECT id FROM films WHERE title=?", (title,)).fetchone()[0]
    c.execute("INSERT OR IGNORE INTO film_tags(film_id,tag_id) VALUES(?,?)", (fid, tid))
c.commit()
r = ask("Do I have any BGGG scenes?")
assert r["direct"] and r["rows"][0][0] == 1 and "Yes" in r["answer"] and "S1" in r["answer"] and "BGGG" in r["answer"], r
assert "H1" not in r["answer"]  # film shouldn't be listed for a scenes question
r = ask("Do I have any BGGG films?")
assert r["direct"] and r["rows"][0][0] == 1 and "H1" in r["answer"], r
r = ask("films tagged BGGG")
assert r["direct"] and r["rows"][0][0] == 1, r
r = ask("Do I have any BGGG and Horror scenes?")
assert r["direct"] and r["rows"][0][0] == 1 and "S1" in r["answer"], r
# a known tag with zero matches in the requested scope is a real answer, not an LLM fallback
c.execute("INSERT OR IGNORE INTO tags(name,category) VALUES('ZZEmpty','Content')")
c.commit()
r = ask("Do I have any ZZEmpty scenes?")
assert r["direct"] and r["rows"][0][0] == 0 and "don't have" in r["answer"].lower(), r
# unknown term is not claimed as a built-in miss
assert R.ask_direct(c, "Do I have any ZZZNOTATAG scenes?") is None

print("OK test_ask_direct")
