"""Upgrade file: one of an item's files is swapped for a better copy. The item keeps its id, tags, cast, poster, hand
edits and watch data; resolution, size, fingerprint and runtime follow the new file; the old file goes to the Recycle
Bin (a fake recycler here, nothing is deleted for real); no exclusion blocks the new file and a rescan adds no
duplicate; sidecar subtitles are only renamed when asked. Temp folders only. Run: python tests/test_upgrade_file.py"""
import os, sys, struct, tempfile, time, json
d = tempfile.mkdtemp(); os.environ["FILMTAGGER_DB"] = os.path.join(d, "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db, upgrade

def box(t, payload): return struct.pack(">I", 8 + len(payload)) + t + payload
def mp4(path, w, h, seconds, pad=0):
    """A tiny MP4 header (no real video) that the built-in readers understand: size w x h, duration seconds."""
    mvhd = box(b"mvhd", bytes(12) + struct.pack(">II", 1000, int(seconds * 1000)) + bytes(80))
    tkhd = box(b"tkhd", bytes(76) + struct.pack(">II", w << 16, h << 16))
    hdlr = box(b"hdlr", bytes(8) + b"vide" + bytes(12))
    data = box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", mvhd + box(b"trak", tkhd + box(b"mdia", hdlr))) + box(b"free", bytes(pad))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh: fh.write(data)
    return path
def mk(p, txt="x"):
    os.makedirs(os.path.dirname(p), exist_ok=True); open(p, "w").write(txt); return p

root = tempfile.mkdtemp(); outside = tempfile.mkdtemp()
c = db.connect(); db.set_setting(c, "roots", [root]); c.commit()
old = mp4(os.path.join(root, "Film (1999)", "Film (1999) 720p.mp4"), 1280, 720, 5400, pad=100)
sub = mk(os.path.join(root, "Film (1999)", "Film (1999) 720p.en.srt"), "1\n00:00:01,000 --> 00:00:02,000\nHi\n")
other_sub = mk(os.path.join(root, "Film (1999)", "Something else.srt"))
r = db.scan(c, [root]); assert r["added"] == 1, r
fid = c.execute("SELECT id FROM films").fetchone()[0]; file_id = c.execute("SELECT id FROM files").fetchone()[0]
# metadata that must survive: tags, cast, poster, hand edits, watch data, rating, notes
c.execute("UPDATE films SET title='Film', cast_='Ann Actor, Bob Actor', poster='http://x/p.jpg', custom_poster='film-1.jpg', watched=1, "
          "rating=8, notes='mine', runtime=90, edited_fields=?, resolution='720p' WHERE id=?", (json.dumps(["title", "notes"]), fid))
db.add_tag(c, fid, "Heist", "Genre"); c.commit()

# ---- probe and plan (dry run): nothing changes ----
p = upgrade.probe(old); assert (p["width"], p["height"], p["resolution"]) == (1280, 720, "720p") and round(p["duration"]) == 5400, p
new = mp4(os.path.join(outside, "Film.1999.1080p.mp4"), 1920, 1080, 5410, pad=5000)
pl, err = upgrade.plan(c, fid, None, '"' + new + '"')  # one file: no need to choose; quotes from 'Copy as path' are fine
assert not err, err
assert pl["file_id"] == file_id and pl["old"]["resolution"] == "720p" and pl["new"]["resolution"] == "1080p"
assert pl["new"]["size"] > pl["old"]["size"] and not pl["inside_library"] and pl["move_possible"]
assert pl["move_target"] == os.path.join(root, "Film (1999)", "Film.1999.1080p.mp4") and pl["summary"] == "Upgraded 720p → 1080p"
assert [s["path"] for s in pl["sidecars"]] == [sub] and pl["sidecars"][0]["target"].endswith("Film.1999.1080p.en.srt")
assert os.path.exists(old) and os.path.exists(new) and c.execute("SELECT path FROM files").fetchone()[0] == old
# refusals
assert upgrade.plan(c, fid, None, "")[1][1] == 400
assert upgrade.plan(c, fid, None, "relative.mkv")[1][1] == 400
assert upgrade.plan(c, fid, None, os.path.join(outside, "nope.mkv"))[1][1] == 400
assert upgrade.plan(c, fid, None, mk(os.path.join(outside, "notes.txt")))[1][1] == 400
assert upgrade.plan(c, fid, None, old)[1][1] == 400
assert upgrade.plan(c, fid, 999999, new)[1][1] == 400
assert upgrade.plan(c, 999999, None, new)[1][1] == 404
other = mp4(os.path.join(root, "Other (2000)", "Other (2000).mp4"), 640, 480, 60); db.scan(c, [root])
assert "already in the library" in upgrade.plan(c, fid, None, other)[1][0]

# ---- an old exclusion that would match the new file (removed once before) ----
c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at) VALUES(?,?,?,?,?,?)",
          (os.path.join(root, "Old place", "Film.1999.1080p.mp4"), os.path.getsize(new), None, "Film", None, time.time()))
keep_x = c.execute("INSERT INTO excluded(path,size,oshash,title,kind,removed_at) VALUES(?,?,?,?,?,?)",
                   (os.path.join(root, "Unrelated.mkv"), 123, None, "Unrelated", None, time.time())).lastrowid
c.commit()
assert db.is_excluded(c, os.path.join(root, "Film (1999)", "Film.1999.1080p.mp4"), os.path.getsize(new))

# ---- apply with move into the old file's folder ----
bin_ = []
def fake_recycle(path): bin_.append(path); os.remove(path); return None
res, err = upgrade.apply(c, fid, file_id, new, move=True, recycler=fake_recycle)
assert not err, err
target = os.path.join(root, "Film (1999)", "Film.1999.1080p.mp4")
assert res["summary"] == "Upgraded 720p → 1080p" and res["moved"] and res["path"] == target and res["recycled"]
assert bin_ == [old] and not os.path.exists(old) and os.path.exists(target) and not os.path.exists(new)
assert res["blockers_removed"] == 1 and c.execute("SELECT COUNT(*) FROM excluded WHERE id=?", (keep_x,)).fetchone()[0] == 1
x = c.execute("SELECT id, film_id, path, size, oshash, missing FROM files WHERE film_id=?", (fid,)).fetchall()
assert len(x) == 1 and x[0]["id"] == file_id and x[0]["path"] == target and x[0]["size"] == os.path.getsize(target) and x[0]["missing"] == 0
assert x[0]["oshash"] == db.file_hash(target)
f = c.execute("SELECT * FROM films WHERE id=?", (fid,)).fetchone()
assert (f["title"], f["cast_"], f["poster"], f["custom_poster"], f["watched"], f["rating"], f["notes"]) == \
       ("Film", "Ann Actor, Bob Actor", "http://x/p.jpg", "film-1.jpg", 1, 8, "mine")
assert json.loads(f["edited_fields"]) == ["title", "notes"]
assert (f["resolution"], f["res_width"], f["res_height"]) == ("1080p", 1920, 1080)
assert f["runtime"] == 90  # 5410 s rounds to 90 min: unchanged
assert [t[0] for t in c.execute("SELECT t.name FROM film_tags ft JOIN tags t ON t.id=ft.tag_id WHERE ft.film_id=?", (fid,))] == ["Heist"]
# subtitles: listed, not renamed until asked
assert os.path.exists(sub) and [s["path"] for s in res["sidecars"]] == [sub]
done, errs = upgrade.rename_sidecars(c, fid, file_id, old, [sub])
assert not errs and done == [dict(path=sub, target=os.path.join(root, "Film (1999)", "Film.1999.1080p.en.srt"))]
assert not os.path.exists(sub) and os.path.exists(done[0]["target"]) and os.path.exists(other_sub)
assert upgrade.rename_sidecars(c, fid, file_id, target, [target])[1]  # the current file is not a replaced file
# rescan: no duplicate, nothing skipped, nothing missing
n_films = c.execute("SELECT COUNT(*) FROM films").fetchone()[0]
r = db.scan(c, [root]); assert r["added"] == 0 and r["relinked"] == 0 and r["missing_files"] == 0, r
assert c.execute("SELECT COUNT(*) FROM films").fetchone()[0] == n_films

# ---- hand-edited resolution and runtime are kept; runtime that came from the old file follows the new one ----
v2 = mp4(os.path.join(root, "Two (2001)", "Two (2001).mp4"), 720, 480, 3000); db.scan(c, [root])
fid2, fl2 = c.execute("SELECT film_id, id FROM files WHERE path=?", (v2,)).fetchone()
c.execute("UPDATE films SET runtime=50, resolution='SD', edited_fields=? WHERE id=?", (json.dumps(["resolution"]), fid2)); c.commit()
n2 = mp4(os.path.join(root, "Two (2001)", "Two (2001) HD.mp4"), 1280, 720, 6000, pad=10)  # inside the library already
pl, err = upgrade.plan(c, fid2, fl2, n2); assert not err and pl["inside_library"] and pl["move_target"] is None
res, err = upgrade.apply(c, fid2, fl2, n2, move=True, recycler=fake_recycle)  # move is ignored inside the library
assert not err and not res["moved"] and res["path"] == n2 and res["resolution_kept_hand_edit"] == "720p" and res["runtime"] == 100
f = c.execute("SELECT resolution, res_width, runtime FROM films WHERE id=?", (fid2,)).fetchone()
assert tuple(f) == ("SD", 1280, 100)

# ---- the Recycle Bin is not available: the old file stays and is put on the Removed items list ----
v3 = mp4(os.path.join(root, "Three (2002)", "Three (2002).mp4"), 640, 360, 600); db.scan(c, [root])
fid3, fl3 = c.execute("SELECT film_id, id FROM files WHERE path=?", (v3,)).fetchone()
n3 = mp4(os.path.join(root, "Three (2002)", "Three (2002) 1080p.mp4"), 1920, 1080, 600, pad=20)
res, err = upgrade.apply(c, fid3, fl3, n3, recycler=lambda p: "Removable drives have no Recycle Bin")
assert not err and not res["recycled"] and res["recycle_error"] and os.path.exists(v3)
assert c.execute("SELECT COUNT(*) FROM excluded WHERE path=?", (v3,)).fetchone()[0] == 1
n_films = c.execute("SELECT COUNT(*) FROM films").fetchone()[0]
r = db.scan(c, [root]); assert r["added"] == 0 and r["skipped_removed"] == 1, r
assert c.execute("SELECT COUNT(*) FROM films").fetchone()[0] == n_films

# ---- several files: only the chosen one changes; same name as the old file: old leaves first ----
a = mp4(os.path.join(root, "Four (2003)", "Four (2003) CD1.mp4"), 640, 360, 600)
b = mp4(os.path.join(root, "Four (2003)", "Four (2003) CD2.mp4"), 640, 360, 600, pad=1); db.scan(c, [root])
fa = c.execute("SELECT film_id FROM files WHERE path=?", (a,)).fetchone()[0]
c.execute("UPDATE files SET film_id=? WHERE path=?", (fa, b)); c.execute("DELETE FROM films WHERE id NOT IN (SELECT film_id FROM files)"); c.commit()
fb = c.execute("SELECT id FROM files WHERE path=?", (b,)).fetchone()[0]
nb = mp4(os.path.join(outside, "Four (2003) CD2.mp4"), 1920, 1080, 600, pad=30)
assert "Choose which file" in upgrade.plan(c, fa, None, nb)[1][0]
pl, err = upgrade.plan(c, fa, fb, nb); assert not err and pl["same_name_as_old"] and pl["move_possible"]
if os.name != "nt":  # here: no Recycle Bin -> refused before anything moves
    e = upgrade.apply(c, fa, fb, nb, move=True, recycler=fake_recycle)[1]; assert e[1] == 400 and "same name" in e[0] and os.path.exists(b) and os.path.exists(nb)
upgrade.recycle_problem = lambda p: None
assert upgrade.apply(c, fa, fb, nb, move=True, recycler=lambda p: "nope")[1][1] == 500 and os.path.exists(b) and os.path.exists(nb)
res, err = upgrade.apply(c, fa, fb, nb, move=True, recycler=fake_recycle)
assert not err, err
assert res["path"] == b and os.path.exists(b) and not os.path.exists(nb) and upgrade.probe(b)["resolution"] == "1080p"
assert sorted(r[0] for r in c.execute("SELECT path FROM files WHERE film_id=?", (fa,))) == sorted([a, b])
print("OK test_upgrade_file")
