"""A hand-set person photo (upload / URL / crop) is a protected hand edit: ThePornDB and TMDB refreshes never change its
photo sources, a replaced hand photo goes to posters/people/backup/, merging keeps it. Run: python tests/test_person_photo_protect.py"""
import os, sys, io, json, tempfile, urllib.request, urllib.parse
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, db, tpdb, people as P
cl = app.app.test_client()
# ThePornDB performer with an image, then Kyle uploads his own photo
c = db.connect(); c.execute("INSERT INTO films(title,kind,cast_) VALUES('S','scene','Pat Perf')"); db.set_setting(c, "tpdb_key", "t"); c.commit()
perf = {"id": "z", "name": "Pat Perf", "image": "https://e/one.jpg", "extras": {}}
tpdb.call_retry = lambda path, tok, **k: {"data": [perf]} if path == "/performers" else {"data": perf}
pid = c.execute("INSERT INTO people(name,name_norm) VALUES('Pat Perf','pat perf')").lastrowid; c.commit()
cl.get(f"/api/person/{pid}?refresh=1")
row = lambda i=pid: c.execute("SELECT * FROM people WHERE id=?", (i,)).fetchone()
assert row()["image_url"] == "https://e/one.jpg" and not P.photo_locked(row())
assert cl.post(f"/api/person/{pid}/photo/upload", data={"photo": (io.BytesIO(b"MINE1"), "a.jpg")}, content_type="multipart/form-data").json["ok"]
assert P.photo_locked(row()) and "photo" in json.loads(row()["edited_fields"])
first = row()["photo_file"]
# refresh / career job / Load: ThePornDB has a new image -> the hand photo and its sources stay
perf["image"] = "https://e/two.jpg"
cl.get(f"/api/person/{pid}?refresh=1")
assert row()["image_url"] == "https://e/one.jpg" and row()["photo_file"] == first
P.apply_tpdb(c, row(), perf); P.apply_tpdb(c, row(), perf, fill_only=True)
assert row()["image_url"] == "https://e/one.jpg" and row()["photo_file"] == first
assert cl.get(cl.get(f"/api/person/{pid}").json["person"]["photo"]).data == b"MINE1"
# a second upload replaces it: the first file is moved to backup/, never deleted or overwritten
assert cl.post(f"/api/person/{pid}/photo/upload", data={"photo": (io.BytesIO(b"MINE2"), "b.jpg")}, content_type="multipart/form-data").json["ok"]
pdir = os.path.join(app.POSTERS_DIR, "people")
assert row()["photo_file"] != first and not os.path.exists(os.path.join(pdir, first))
assert open(os.path.join(pdir, "backup", first), "rb").read() == b"MINE1"
# merging: the kept person takes over a hand photo from the merged one
c.execute("INSERT INTO people(name,name_norm) VALUES('Keep Me','keep me')"); c.commit(); kid = c.execute("SELECT id FROM people WHERE name_norm='keep me'").fetchone()[0]
assert cl.post(f"/api/person/{kid}/merge", json={"other": pid}).json["ok"]
assert row(kid)["photo_file"] == row()["photo_file"] and P.photo_locked(row(kid))
print("ok")
