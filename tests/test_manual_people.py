import os, sys, io, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb
cl = app.app.test_client()
assert cl.post("/api/people/new", data={"name": "  "}).status_code == 400
r = cl.post("/api/people/new", data={"name": "Jane Indie", "aliases": "J. Indie, Janie", "nationality": "Dutch", "height": "170cm",
            "links": "IAFD: https://www.iafd.com/person.rme/id=x\nhttps://x.com/janeindie", "bio": "Hand bio",
            "photo": (io.BytesIO(b"\x89PNG fake"), "me.png")}, content_type="multipart/form-data").json
pid = r["id"]
p = cl.get(f"/api/person/{pid}").json
assert p["status"] == "ok" and p["person"]["source"].startswith("Manual"), p
pp = p["person"]
assert pp["aliases"] == ["J. Indie", "Janie"] and pp["nationality"] == "Dutch" and pp["photo"]
assert pp["links"] == {"IAFD": "https://www.iafd.com/person.rme/id=x", "X": "https://x.com/janeindie"} and pp["iafd"]
assert {"bio", "nationality", "height", "aliases", "links_json", "photo"} <= set(pp["edited"])
assert cl.get(pp["photo"]).data == b"\x89PNG fake"
# autocomplete/search finds a manual person with no films
assert [x["name"] for x in cl.get("/api/people/search?q=jane ind").json["people"]] == ["Jane Indie"]
# link to a scene via cast; TPDB refresh never overwrites hand fields or the manual photo
c = db.connect(); c.execute("INSERT INTO films(title,kind,cast_) VALUES('S','scene','Jane Indie')"); db.set_setting(c, "tpdb_key", "t"); c.commit()
perf = {"id": "z", "name": "Jane Indie", "bio": "TPDB bio", "image": "https://e/i.jpg", "extras": {"nationality": "German", "birthplace": "Utrecht"}}
tpdb.call_retry = lambda path, tok, **k: {"data": [perf]} if path == "/performers" else {"data": perf}
pp = cl.get(f"/api/person/{pid}?refresh=1").json["person"]
assert pp["bio"] == "Hand bio" and pp["nationality"] == "Dutch" and pp["place"] == "Utrecht"
assert cl.get(pp["photo"]).data == b"\x89PNG fake"
# edit links via JSON
cl.post(f"/api/person/{pid}/edit", json={"links": "https://onlyfans.com/jane"})
assert cl.get(f"/api/person/{pid}").json["person"]["links"] == {"OnlyFans": "https://onlyfans.com/jane"}
print("ok")
