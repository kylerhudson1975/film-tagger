"""Smart tagging (AI) bridge - works with any AI chatbot or API.

Typical use: export the queue, paste SMART_TAG_PROMPT.txt + the exported JSON into any AI chat,
save its JSON reply to a file, then import it.

  python smarttag.py export-queue [out.json] [--wait SECONDS] [--no-lookup] [--retry]
                                                  write the smart-tag queue (default: smart_queue.json); first looks up
                                                  birth dates for queued films' cast that lack one (through the running app,
                                                  else here; waits up to 600 s) and lists per film who still lacks one
                                                  ("cast_missing_birthdate"); names not found before are tried again after
                                                  7 days (--retry: now); a scene with no cast lists "(no performers listed)";
                                                  adult films get "age_check" (each performer's age on the release date)
  python smarttag.py import-tags file.json        import AI tags -> pending approval in the app
  python smarttag.py status                       show queue / pending counts
  python smarttag.py clear-queue                  empty the queue
  python smarttag.py backfill-primary-genre       fill empty primary genres from stored TMDB data (no network)
  python smarttag.py export-aznude-queue [out.json]  write the AZNude Status queue (default: aznude_queue.json)
  python smarttag.py import-aznude results.json   write AZNude Status results and clear those films from the AZNude queue

export-aznude-queue format: [{"id", "title", "original_title", "alt_titles": [...], "akas": [...], "year", "director",
  "cast": [...], "path", "paths": [...], "current_status", "current_url"}, ...]
import-aznude format: [{"film_id": 12, "status": "Hardcore"|"Softcore"|"Not found"|null, "url": "...", "note": "..."}, ...]
  A hand-set AZNude Status is never overwritten (url/note are still filled). Never changes Primary genre or tags.

import-tags format: [{"film_id": 12, "primary_genre": "Thriller", "tags": [{"name": "heist", "category": "Themes"}, ...]}, ...]
Imported tags get source='ai', status='pending' until approved in the UI. Films are removed from the
queue once their tags are imported (use --keep-queue to keep them).
"""
import os, sys, json, time, db

def lookup_birthdates(wait, retry=False):
    """Birth dates for the queued films' cast before exporting: asks the running app (same database) so its rate limits
    and background job are shared; runs the lookup here when the app is not running."""
    import urllib.request
    port = 5050
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "port.txt"), encoding="utf-8") as f: port = int(f.read().strip())
    except (OSError, ValueError): pass
    base = f"http://127.0.0.1:{port}/api/birthdates/"
    def call(path, body=None):
        rq = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(rq, timeout=120) as r: return json.loads(r.read().decode("utf-8"))  # a big queue takes a while to check
    try:
        r = call("queue-lookup", {"retry": retry}); st = lambda: call("status")["queued"]
        same = os.path.normcase(os.path.abspath(r.get("db_path") or db.DB_PATH)) == os.path.normcase(os.path.abspath(db.DB_PATH))
        if not same: raise OSError("the running app uses another database")
        print(f"Birth-date lookup (Film Tagger app on port {port}): {r.get('added', 0)} name(s) added")
        if not r.get("added") and not r.get("running"): return
    except (OSError, ValueError):
        import birthdates as bd
        if not bd.enabled(): return
        c = db.connect(); bd.ensure(c); c.close()
        import threading
        n = len(bd.queue_todo(db.connect(), bd.queued_film_ids(db.connect()), retry))
        if not n: return
        print(f"Birth-date lookup (app not running, looking up here): {n} name(s)")
        th = threading.Thread(target=bd.queue_lookup, args=(db.connect,), kwargs=dict(sync=True, retry=retry), daemon=True); th.start()
        st = lambda: dict(bd.PRIO, left=len(bd.PRIO["todo"]), running=th.is_alive())
    end, last = time.time() + wait, None
    while True:
        q = st()
        if not q.get("running"): break
        if time.time() > end:
            print(f"  still looking up ({q.get('left', 0)} left); exporting now - run export-queue again later for the rest"); return
        msg = f"  {q.get('done', 0)}/{q.get('total', 0)} checked, {q.get('filled', 0)} found" + (f" (now: {q['current']})" if q.get("current") else "")
        if msg != last: print(msg, flush=True); last = msg
        time.sleep(3)
    q = st()
    for x in q.get("results", [])[-q.get("done", 0):] if q.get("done") else []:
        if x.get("birthday"): print(f"  found: {x['name']} {x['birthday']} ({(x.get('source') or '').split(' ')[0]})")

WHY = {"title_as_name": "a title stored as a person (fix the cast: python scenecast.py report)", "no_cast": "scenes with no performers listed",
       "ambiguous": "in the birth-date review list", "not_found": "not found (tried again after 7 days, or now with --retry)",
       "no_birthday": "no exact date found (tried again after 7 days, or now with --retry)", "hand_edited": "birth date cleared by hand",
       "reviewed_none": "marked 'none' in review", "not_looked_up": "not looked up yet (lookup still running or off)", "error": "lookup errors"}

def missing_summary(q):
    """One line: how many queued films still have cast without a birth date, and why (by lookup status)."""
    from collections import Counter
    films = [f for f in q if f["cast_missing_birthdate"]]
    if not films: return f"All {len(q)} queued film(s) have a birth date for every cast member."
    st = Counter(); seen = set()
    for f in films:
        for m in f["cast_missing_birthdate"]:
            k = (m["name"].casefold(), m["status"])
            if k not in seen: seen.add(k); st[m["status"]] += 1
    return f"{len(films)} of {len(q)} queued film(s) still have cast without a birth date: " + \
        "; ".join(f"{n} {WHY.get(s, s)}" for s, n in st.most_common())

def main(a):
    if not a or a[0] in ("-h", "--help"): print(__doc__); return 0
    c = db.connect(); cmd = a[0]
    if cmd == "export-queue":
        pos = [x for i, x in enumerate(a[1:], 1) if not x.startswith("--") and a[i - 1] != "--wait"]
        out = pos[0] if pos else "smart_queue.json"
        wait = float(a[a.index("--wait") + 1]) if "--wait" in a else 600.0
        if "--no-lookup" not in a and c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0]: lookup_birthdates(wait, "--retry" in a)
        q = db.export_queue(c)
        with open(out, "w", encoding="utf-8") as f: json.dump(q, f, indent=2, ensure_ascii=False)
        print(f"Exported {len(q)} queued film(s) to {out} (database: {db.DB_PATH})")
        for f0 in q:
            if f0["cast_missing_birthdate"]:
                print(f"  {f0['title']} ({f0['year'] or '?'}): no birth date for " + ", ".join(f"{m['name']} [{m['status']}]" for m in f0["cast_missing_birthdate"]))
        print(missing_summary(q))
        unv = [f for f in q if (f.get("age_check") or {}).get("status") in ("no_scene_date", "underage")]
        if unv:
            print(f"Age at filming not verified for {len(unv)} adult film(s) (no release date, or someone under 18 on it): "
                  "see age_check in the export; don't tag them until it is 'ok'.")
    elif cmd == "import-tags":
        if len(a) < 2: print("usage: smarttag.py import-tags file.json"); return 2
        with open(a[1], encoding="utf-8-sig") as f: data = json.load(f)
        if isinstance(data, dict): data = data.get("items", [data])
        r = db.import_ai_tags(c, data, dequeue="--keep-queue" not in a)
        print(f"Imported {r['tags_added']} pending AI tag(s).", f"Unknown film ids skipped: {r['skipped_film_ids']}" if r["skipped_film_ids"] else "")
    elif cmd == "backfill-primary-genre":
        r = db.backfill_primary_genre(c)
        print(f"Primary genre filled for {r['filled']} film(s) ({r['from_tmdb_genres']} from saved TMDB genres, {r['from_tmdb_tags']} from TMDB genre tags).",
              f"Left blank (no stored TMDB genres): {r['no_cache']} ({r['no_cache_with_tmdb_id']} have a TMDB id - use Fetch from TMDB to fill).",
              f"Already set: {r['already_set']}. Hand-edited (untouched): {r['user_edited']}.")
    elif cmd == "export-aznude-queue":
        out = a[1] if len(a) > 1 else "aznude_queue.json"
        q = db.export_aznude_queue(c)
        with open(out, "w", encoding="utf-8") as f: json.dump(q, f, indent=2, ensure_ascii=False)
        print(f"Exported {len(q)} AZNude-queued film(s) to {out}")
    elif cmd == "import-aznude":
        if len(a) < 2: print("usage: smarttag.py import-aznude results.json"); return 2
        with open(a[1], encoding="utf-8-sig") as f: data = json.load(f)
        if isinstance(data, dict): data = data.get("items", data.get("results", [data]))
        r = db.import_aznude(c, data, dequeue="--keep-queue" not in a)
        print(f"AZNude Status set for {r['set']} film(s).",
              f"Hand-edited (status kept): {r['protected']}." if r["protected"] else "",
              f"Unknown film ids skipped: {r['skipped_film_ids']}." if r["skipped_film_ids"] else "",
              ("Errors: " + "; ".join(r["errors"])) if r["errors"] else "")
        if r["errors"]: return 1
    elif cmd == "status":
        print("database:", db.DB_PATH)
        print("queue:", c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0],
              "| films with pending AI tags:", c.execute("SELECT COUNT(DISTINCT film_id) FROM film_tags WHERE status='pending'").fetchone()[0],
              "| AZNude queue:", c.execute("SELECT COUNT(*) FROM aznude_queue").fetchone()[0])
    elif cmd == "clear-queue":
        c.execute("DELETE FROM smart_queue"); c.commit(); print("Queue cleared.")
    else: print(__doc__); return 2
    return 0

if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
