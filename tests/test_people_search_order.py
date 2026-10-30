"""People search order: one word is a first or last name (bigger count of hits with it as first vs last word, tie =
first); those people first, then the rest A-Z. Run: python tests/test_people_search_order.py"""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db
cl = app.app.test_client()
c = db.connect()
db.set_setting(c, "roots", [r"D:\\Movies"])
c.execute("INSERT INTO films(title,kind,cast_,year) VALUES('A','film',?,2020)",
          ("Summer Renee, Renee Summers, Renée Duval, Renee Estevez, Renee LaRue, Ann Johnson, Zed Johnson, Bob Johnson, Johnson Lee, Tom Johnsonville",))
c.commit()
names = lambda q: [p["name"] for p in cl.get("/api/people/search", query_string=dict(q=q)).json["people"]]
assert names("renee") == ["Renée Duval", "Renee Estevez", "Renee LaRue", "Renee Summers", "Summer Renee"], names("renee")
assert names("johnson") == ["Ann Johnson", "Bob Johnson", "Zed Johnson", "Johnson Lee", "Tom Johnsonville"], names("johnson")
assert names("renee summers") == ["Renee Summers"]
assert names("enee") == ["Renée Duval", "Renee Estevez", "Renee LaRue", "Renee Summers", "Summer Renee"]
print("ok")
