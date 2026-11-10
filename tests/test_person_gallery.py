"""Performer galleries: folder matching (name/alias/ambiguous/none), linking, listing, hide. Run: python tests/test_person_gallery.py"""
import os, sys, json, tempfile
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import persongallery as pg

# --- pure matching ---
P = [dict(id=1, name="Crystal Clark", aliases=["Krystal C"], adult=True),
     dict(id=2, name="Andi Rose", aliases=["Andi"], adult=True),           # single-word alias never links
     dict(id=3, name="Renée Blue", aliases=[], adult=True),
     dict(id=4, name="Sam Twin", aliases=[], adult=True), dict(id=5, name="Sam Twin", aliases=[], adult=True),
     dict(id=6, name="Jo Main", aliases=[], adult=False), dict(id=7, name="Lee Star", aliases=["Jo Main"], adult=True),
     dict(id=8, name="Pat One", aliases=["Shared Name"], adult=True), dict(id=9, name="Pat Two", aliases=["Shared Name"], adult=True)]
r = {m["folder"]: m for m in pg.match_folders(["crystal  CLARK ", "Krystal_C", "Andi", "renee blue", "Sam Twin", "Jo Main", "Shared Name", "Nobody Here", "Crystál Clark"], P)}
assert r["crystal  CLARK "]["kind"] == "exact" and r["crystal  CLARK "]["person_id"] == 1
assert r["Crystál Clark"]["kind"] == "exact" and r["Crystál Clark"]["person_id"] == 1
assert r["Krystal_C"]["kind"] == "alias" and r["Krystal_C"]["person_id"] == 1
assert r["Andi"]["kind"] == "none"
assert r["renee blue"]["kind"] == "exact" and r["renee blue"]["person_id"] == 3
assert r["Sam Twin"]["kind"] == "ambiguous" and [c["id"] for c in r["Sam Twin"]["candidates"]] == [4, 5]
assert r["Jo Main"]["kind"] == "alias" and r["Jo Main"]["person_id"] == 7      # adult performer preferred over mainstream namesake
assert r["Shared Name"]["kind"] == "ambiguous" and r["Shared Name"]["person_id"] is None
assert r["Nobody Here"]["kind"] == "none" and r["Nobody Here"]["candidates"] == []
assert pg.key(" Crystál_Clark. ") == "crystal clark"
assert pg.rel_path("Sub\\photo.jpg") == "Sub/photo.jpg"
assert pg.partition(["a.jpg", "b.jpg", "c.jpg"], {"b.jpg", "gone.jpg"}) == (["a.jpg", "c.jpg"], ["b.jpg"])
assert pg.listing(["a.jpg", "b.jpg"], {"b.jpg"})["count"] == 1 and pg.listing(["a.jpg", "b.jpg"], {"b.jpg"})["hidden_images"] == ["b.jpg"]


# --- API: survey + auto-link + gallery listing ---
import app, db
cl = app.app.test_client(); c = db.connect()
c.execute("INSERT INTO films(title,kind,cast_) VALUES('Scene A','scene','Crystal Clark, Brooklyn Gray')")
c.execute("INSERT INTO films(title,kind,cast_) VALUES('Scene B','scene','Twin Name')"); c.commit()
pid = cl.get("/api/person/lookup?name=Crystal Clark").json["id"]
t1 = cl.get("/api/person/lookup?name=Twin Name").json["id"]
c.execute("INSERT INTO people(name, name_norm, aliases) VALUES('Other Twin','other twin',?)", (json.dumps(["Twin Name"]),)); c.commit()
root = os.path.join(d, "Porn Stars")
for f, n in (("Crystal Clark", 3), ("Brooklyn Gray", 2), ("Twin Name", 1), ("Unknown Girl", 1)):
    os.makedirs(os.path.join(root, f))
    for i in range(n): open(os.path.join(root, f, f"{i:04d}.jpg"), "wb").write(b"x")
open(os.path.join(root, "Andi.jpg"), "wb").write(b"x")
r = cl.post("/api/people/gallery_link", json={"root": root}).json      # dry run: nothing linked
assert r["totals"]["linked"] == 0 and r["loose_images"] == ["Andi.jpg"]
r = cl.post("/api/people/gallery_link", json={"root": root, "apply": True}).json
k = {m["folder"]: m for m in r["folders"]}
assert k["Crystal Clark"]["linked"] and k["Crystal Clark"]["person_id"] == pid and k["Crystal Clark"]["count"] == 3
assert k["Brooklyn Gray"]["linked"]                                     # credited, no profile row yet: row created
assert k["Twin Name"]["kind"] == "ambiguous" and not k["Twin Name"]["linked"]
assert k["Unknown Girl"]["kind"] == "none" and not k["Unknown Girl"]["linked"]
assert r["totals"] == dict(folders=4, linked=2, exact=2, alias=0, ambiguous=1, none=1), r["totals"]
g = cl.get(f"/api/person/{pid}/gallery").json
assert g["count"] == 3 and g["images"] == ["0000.jpg", "0001.jpg", "0002.jpg"]
assert cl.get(f"/api/person/{pid}").json["person"]["gallery_dir"] == os.path.join(root, "Crystal Clark")
open(os.path.join(root, "Crystal Clark", "0003.jpg"), "wb").write(b"x")   # added later: shows up
assert cl.get(f"/api/person/{pid}/gallery").json["count"] == 4
assert cl.get(f"/api/person/{pid}/gallery/image?name=0000.jpg").status_code == 200
assert cl.get(f"/api/person/{pid}/gallery/image?name=../Andi.jpg").status_code == 404
# set / clear by hand
assert cl.post(f"/api/person/{t1}/gallery/folder", json={"path": os.path.join(d, "nope")}).status_code == 400
assert cl.post(f"/api/person/{t1}/gallery/folder", json={"path": os.path.join(root, "Twin Name")}).json["count"] == 1
assert cl.post(f"/api/person/{t1}/gallery/folder", json={"path": ""}).json["gallery_dir"] is None
assert cl.get(f"/api/person/{t1}/gallery").json["folder"] is None
# re-running never overwrites a different folder chosen by hand
other = os.path.join(d, "Mine"); os.makedirs(other)
cl.post(f"/api/person/{pid}/gallery/folder", json={"path": other})
r = cl.post("/api/people/gallery_link", json={"root": root, "apply": True}).json
assert not {m["folder"]: m for m in r["folders"]}["Crystal Clark"]["linked"]
assert cl.get(f"/api/person/{pid}").json["person"]["gallery_dir"] == other
assert sorted(os.listdir(os.path.join(root, "Crystal Clark"))) == ["0000.jpg", "0001.jpg", "0002.jpg", "0003.jpg"]  # files untouched
# --- hide from gallery: files never moved, hide list persists, leftover paths are harmless ---
hid_pid = cl.get("/api/person/lookup?name=Hide Testers").json["id"]
hroot = os.path.join(d, "Hide Gallery"); os.makedirs(hroot)
for n in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
    open(os.path.join(hroot, n), "wb").write(b"x")
assert cl.post(f"/api/person/{hid_pid}/gallery/folder", json={"path": hroot}).json["count"] == 4
g = cl.get(f"/api/person/{hid_pid}/gallery").json
assert g["count"] == 4 and g["images"] == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
assert g.get("hidden_images") == [] and g.get("hidden_count", 0) == 0
r = cl.post(f"/api/person/{hid_pid}/gallery/hide", json={"name": "b.jpg", "hidden": True}).json
assert r["count"] == 3 and r["images"] == ["a.jpg", "c.jpg", "d.jpg"]
assert r["hidden_images"] == ["b.jpg"] and r["hidden_count"] == 1
assert sorted(os.listdir(hroot)) == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]  # file still there
# persists across visits; a new file shows up; the hide remains
open(os.path.join(hroot, "e.jpg"), "wb").write(b"x")
g = cl.get(f"/api/person/{hid_pid}/gallery").json
assert g["count"] == 4 and g["images"] == ["a.jpg", "c.jpg", "d.jpg", "e.jpg"] and g["hidden_images"] == ["b.jpg"]
# unhide
r = cl.post(f"/api/person/{hid_pid}/gallery/hide", json={"name": "b.jpg", "hidden": False}).json
assert r["count"] == 5 and r["images"] == ["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"] and r["hidden_count"] == 0
# hide again, then switch folder: leftover hide path is harmless (no matching file)
cl.post(f"/api/person/{hid_pid}/gallery/hide", json={"name": "b.jpg", "hidden": True})
other2 = os.path.join(d, "OtherHide"); os.makedirs(other2)
open(os.path.join(other2, "z.jpg"), "wb").write(b"x")
cl.post(f"/api/person/{hid_pid}/gallery/folder", json={"path": other2})
g = cl.get(f"/api/person/{hid_pid}/gallery").json
assert g["count"] == 1 and g["images"] == ["z.jpg"] and g["hidden_images"] == [] and g["hidden_count"] == 0
# original folder again: hide still applies
cl.post(f"/api/person/{hid_pid}/gallery/folder", json={"path": hroot})
g = cl.get(f"/api/person/{hid_pid}/gallery").json
assert "b.jpg" in g["hidden_images"] and "b.jpg" not in g["images"] and g["count"] == 4
# clear folder: hide list is harmless
assert cl.post(f"/api/person/{hid_pid}/gallery/folder", json={"path": ""}).json["gallery_dir"] is None
g = cl.get(f"/api/person/{hid_pid}/gallery").json
assert g["folder"] is None and g["count"] == 0 and g["hidden_images"] == []
# set back, traversal is rejected, files untouched
cl.post(f"/api/person/{hid_pid}/gallery/folder", json={"path": hroot})
assert cl.post(f"/api/person/{hid_pid}/gallery/hide", json={"name": "../Andi.jpg", "hidden": True}).status_code in (400, 404)
assert cl.post(f"/api/person/{hid_pid}/gallery/hide", json={"name": "nope.jpg", "hidden": True}).status_code == 404
assert sorted(os.listdir(hroot)) == ["a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg"]
# hide a subfolder photo
sub = os.path.join(hroot, "Shoot"); os.makedirs(sub)
open(os.path.join(sub, "s.jpg"), "wb").write(b"x")
r = cl.post(f"/api/person/{hid_pid}/gallery/hide", json={"name": "Shoot/s.jpg", "hidden": True}).json
assert "Shoot/s.jpg" in r["hidden_images"] and "Shoot/s.jpg" not in r["images"]
assert os.path.isfile(os.path.join(sub, "s.jpg"))

print("ok")
