"""Find custom posters of an older Film Tagger library (another data folder, e.g. before a reinstall or a move to a
new install folder) whose films are now in this library without them, and optionally bring them back.

Matching is by video file: an old film and a current film are the same only when they share a file path (case
ignored), the old film maps to exactly one current film, and their TMDB ids agree (or, without TMDB ids on both
sides, title and year agree). Nothing is guessed from titles alone.

    python posterrecover.py OLD_FILMS_DB                 report only (default)
    python posterrecover.py OLD_FILMS_DB --apply         copy + link the safe ones (see below)

--apply only links a poster when: the mapping is certain, the current film has no custom poster, the current film is
not adult (no scene, primary genre not Hardcore/Softcore, on either side), it is not a 'Review by hand' scene, and the
old poster file still exists. The old file is copied (never moved) into this library's posters/ folder under a new
name and marked hand-edited. Everything else is only listed. The images themselves are never opened."""
import argparse, json, os, shutil, sqlite3, sys, time

ADULT_GENRES = {"Hardcore", "Softcore"}

def _ro(path):
    return sqlite3.connect("file:" + os.path.abspath(path).replace("\\", "/").replace(" ", "%20").replace("#", "%23") + "?mode=ro", uri=True)

def _adult(r):
    return (r["kind"] or "") == "scene" or (r["primary_genre"] or "") in ADULT_GENRES

def _same_film(o, n):
    if o["tmdb_id"] and n["tmdb_id"]: return str(o["tmdb_id"]) == str(n["tmdb_id"])
    if o["tmdb_id"] or n["tmdb_id"]: return False
    return (o["title"] or "").strip().casefold() == (n["title"] or "").strip().casefold() and o["year"] == n["year"]

def plan(old_db, cur_c, old_posters_dir=None):
    """-> list of dicts, one per old film with a local custom poster: old_id, title, poster (file name), old_file
    (path or None when gone), new_id, new_title, status, adult, safe (True = --apply links it)."""
    oc = _ro(old_db); oc.row_factory = sqlite3.Row
    old_posters_dir = old_posters_dir or os.path.join(os.path.dirname(os.path.abspath(old_db)), "posters")
    by_path = {}
    for fid, p in cur_c.execute("SELECT film_id, path FROM files"):
        by_path.setdefault(os.path.normcase(p or "").lower(), set()).add(fid)
    out = []
    for o in oc.execute("SELECT id, title, year, kind, primary_genre, tmdb_id, custom_poster FROM films "
                        "WHERE COALESCE(custom_poster,'') LIKE '/posters/%' ORDER BY id"):
        rel = o["custom_poster"][len("/posters/"):]
        src = os.path.join(old_posters_dir, *rel.split("/"))
        it = dict(old_id=o["id"], title=o["title"], poster=os.path.basename(rel), old_file=src if os.path.isfile(src) else None,
                  new_id=None, new_title=None, adult=_adult(o), safe=False)
        cands = set()
        for (p,) in oc.execute("SELECT path FROM files WHERE film_id=?", (o["id"],)): cands |= by_path.get(os.path.normcase(p or "").lower(), set())
        rows = [cur_c.execute("SELECT id, title, year, kind, primary_genre, tmdb_id, custom_poster, COALESCE(notes,'') notes FROM films WHERE id=?", (i,)).fetchone() for i in sorted(cands)]
        rows = [r for r in rows if r]
        if not it["old_file"]: it["status"] = "old poster file missing"
        elif not rows: it["status"] = "no current film with the same video file"
        elif len(rows) > 1: it["status"] = "several current films share its files"
        else:
            n = rows[0]; it.update(new_id=n[0], new_title=n[1])
            nd = dict(zip(("id", "title", "year", "kind", "primary_genre", "tmdb_id", "custom_poster", "notes"), n))
            it["adult"] = it["adult"] or _adult(nd)
            if not _same_film(o, nd): it["status"] = "same file but a different TMDB id / title"
            elif nd["custom_poster"]: it["status"] = "current film already has a custom poster"
            elif "Review by hand" in nd["notes"]: it["status"] = "Review by hand: left alone"
            elif it["adult"]: it["status"] = "adult: listed only, link by hand"
            else: it["status"] = "safe"; it["safe"] = True
        out.append(it)
    oc.close()
    return out

def apply(items, cur_c, posters_dir):
    """Copy and link the safe items. Returns the number linked. Re-checks the current film right before writing."""
    os.makedirs(posters_dir, exist_ok=True); n = 0
    for it in items:
        if not it["safe"]: continue
        r = cur_c.execute("SELECT custom_poster, edited_fields FROM films WHERE id=?", (it["new_id"],)).fetchone()
        if not r or r[0]: it["status"] = "skipped: has a custom poster now"; it["safe"] = False; continue
        ext = os.path.splitext(it["poster"])[1].lower() or ".jpg"
        name = f"film-{it['new_id']}-{int(time.time())}-restored{ext}"; k = 0
        while os.path.exists(os.path.join(posters_dir, name)):
            k += 1; name = f"film-{it['new_id']}-{int(time.time())}-restored{k}{ext}"
        shutil.copy2(it["old_file"], os.path.join(posters_dir, name))
        ed = set(json.loads(r[1] or "[]")); ed.add("custom_poster")
        cur_c.execute("UPDATE films SET custom_poster=?, edited_fields=?, updated_at=? WHERE id=? AND COALESCE(custom_poster,'')=''",
                      ("/posters/" + name, json.dumps(sorted(ed)), time.time(), it["new_id"]))
        cur_c.commit(); it["status"] = "linked"; it["new_poster"] = name; n += 1
    return n

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old_db"); ap.add_argument("--old-posters"); ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    import db
    if os.path.normcase(os.path.abspath(a.old_db)) == os.path.normcase(os.path.abspath(db.DB_PATH)):
        sys.exit("The old database is this library's own database")
    c = db.connect()
    items = plan(a.old_db, c, a.old_posters)
    if a.apply: apply(items, c, os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "posters"))
    if a.json: print(json.dumps(items, ensure_ascii=False, indent=1)); return
    for it in items:
        print(f"[{it['status']}] old #{it['old_id']} {it['title']!r} | {it['poster']} -> " +
              (f"#{it['new_id']} {it['new_title']!r}" if it["new_id"] else "-") + (f" | now {it['new_poster']}" if it.get("new_poster") else ""))
    from collections import Counter
    print("summary:", dict(Counter(it["status"] for it in items)))

if __name__ == "__main__":
    main()
