"""Scene casts that are really a title stored as a person ('Kelsey Kane Cum Inside Me', 'familystrokes 18 03 01 jade amber'):
find them, work out the real performers, and replace them.

Older scans split file names like 'BrattySis Katie Kush Skyler Storm.mp4' by putting the whole text in the cast, so
birth-date lookups for that 'person' could never find anything. Performers come from (best first):
  1. a confident ThePornDB match for the scene (its performer list), else
  2. the file name split against performers already known in the library, plus performers ThePornDB lists for the
     same studio (confirmation only) - accepted only when every word left over is a plain title word, so an unknown
     name in the file name ('Summer Vixen') is never dropped silently.
Uncertain scenes are left unchanged for review. Hand-edited casts are never touched.

  python scenecast.py report [--ids 1,2]                       count title-as-person cast entries (read-only)
  python scenecast.py plan out.json [--ids 1,2] [--tpdb] [--cache cands.json]
                                                               proposals (read-only); --tpdb asks ThePornDB for candidates
  python scenecast.py apply plan.json                          apply the confident proposals in a plan
"""
import json, os, re, sys, time
import db, tpdb

def scene_rows(c, ids=None):
    q = ("SELECT f.id, f.title, f.cast_, f.studio, f.tpdb_id, f.edited_fields, f.notes, "
         "(SELECT path FROM files x WHERE x.film_id=f.id ORDER BY x.id LIMIT 1) AS path FROM films f WHERE f.kind='scene'")
    rows = c.execute(q + " ORDER BY f.id").fetchall()
    return [r for r in rows if ids is None or r["id"] in ids]

def split_cast(v): return [" ".join(x.split()) for x in (v or "").split(",") if x.strip()]

def junk_entries(c, kp=None, ids=None):
    """{film id: [cast entries that look like a title]} for scenes without a ThePornDB id and without a hand-edited cast."""
    kp = kp if kp is not None else db.known_performers(c)
    out = {}
    for r in scene_rows(c, ids):
        if r["tpdb_id"] or "cast_" in json.loads(r["edited_fields"] or "[]"): continue
        bad = [n for n in split_cast(r["cast_"]) if tpdb.looks_like_title(n, kp)]
        if bad: out[r["id"]] = bad
    return out

def _cand_brief(e):
    s = e["scene"]; site = s.get("site") or {}
    return dict(id=str(s.get("id") or s.get("_id") or ""), title=s.get("title"), studio=site.get("name"),
                performers=[p.get("name") for p in s.get("performers") or [] if p.get("name")], score=e["score"], why=e["why"],
                confident=bool(e.get("confident")))

def tpdb_candidates(token, path, fetch=None):
    """ThePornDB candidates for one file (names only, no images)."""
    r = tpdb.candidates(token, path, limit=6, fetch=fetch)
    return [_cand_brief(e) for e in r["results"]]

def propose(c, r, kp, cands=None):
    """-> dict(film_id, old, new, source, confident, why, left) for one scene row."""
    old = split_cast(r["cast_"])
    out = dict(film_id=r["id"], title=r["title"], path=r["path"], old=old, new=None, source=None, confident=False, why="", left=[])
    if not r["path"]: out["why"] = "no file"; return out
    sure = [x for x in cands or [] if x.get("confident") and x.get("performers")]
    if sure:
        best = max(sure, key=lambda x: x["score"])
        out.update(new=[kp.get(n) or n for n in best["performers"]], source=f"tpdb {best['id']}", confident=True,
                   why=f"confident ThePornDB match ({', '.join(best['why'])})")
        return out
    k2 = tpdb.KnownPerformers()
    k2.map = dict(kp.map); k2.first = set(kp.first); k2.vocab = set(kp.vocab)
    confirmed = set()
    for x in cands or []:  # same-studio ThePornDB scenes confirm performer names in the file name
        if "studio" in (x.get("why") or []):
            for n in x.get("performers") or []:
                if tpdb.nrm(n) not in k2.map: k2.add(n); confirmed.add(n)
    n = tpdb.parse_name(r["path"], k2)
    rest = n.get("title") or ""
    perf, left = tpdb.split_performers(rest, k2) if rest else (n["performers"], [])
    perf = n["performers"]
    out.update(new=perf, left=left)
    if not perf: out["why"] = "no known performer name in the file name"; return out
    if not k2.title_only(left):
        out["why"] = f"'{' '.join(k2.name_like(left))}' in the file name may be an unknown performer (rest: {' '.join(left)})"; return out
    used = [p for p in perf if p in confirmed]
    out.update(source="filename" + (" + tpdb studio" if used else ""), confident=True,
               why="known performers" + (f" ({', '.join(used)} confirmed by ThePornDB scenes of this studio)" if used else "") +
                   (f"; rest is title: {' '.join(left)}" if left else ""))
    return out

def still_used(c, name):
    """True when any film credits this name in any role (or a person page has data worth keeping)."""
    cols = list(db.PEOPLE_ROLES.values())
    return bool(c.execute(f"SELECT 1 FROM films f WHERE {' OR '.join(f'person_in_any(f.{x}, ?)' for x in cols)} LIMIT 1", [name] * len(cols)).fetchone())

def apply_plan(c, plan):
    """Apply confident proposals: the cast becomes the real performers (existing people's spelling); hand-edited casts and
    changed rows are skipped. Junk names nobody credits any more lose their lookup row and their empty people row."""
    done, skipped, removed_people, removed_lookups = [], [], 0, 0
    for p in plan:
        if not p.get("confident") or not p.get("new"): continue
        r = c.execute("SELECT cast_, edited_fields, tpdb_id FROM films WHERE id=?", (p["film_id"],)).fetchone()
        if not r or "cast_" in json.loads(r["edited_fields"] or "[]") or r["tpdb_id"] or split_cast(r["cast_"]) != p["old"]:
            skipped.append(p["film_id"]); continue
        c.execute("UPDATE films SET cast_=?, updated_at=? WHERE id=?", (", ".join(p["new"]), time.time(), p["film_id"]))
        done.append(p["film_id"])
        for name in p["old"]:
            if db.fold(name) in {db.fold(x) for x in p["new"]} or still_used(c, name): continue
            k = db.fold(" ".join(name.split()))
            removed_lookups += c.execute("DELETE FROM birthdate_lookup WHERE name_norm=?", (k,)).rowcount
            removed_people += c.execute("DELETE FROM people WHERE name_norm=? AND COALESCE(birthday,'')='' AND COALESCE(edited_fields,'') IN ('','[]') "
                                        "AND tpdb_performer_id IS NULL AND COALESCE(tmdb_person_id,0)<=0 AND merged_into IS NULL "
                                        "AND NOT EXISTS (SELECT 1 FROM people q WHERE q.merged_into=people.id)", (k,)).rowcount
    c.commit()
    return dict(fixed=done, skipped=skipped, removed_people=removed_people, removed_lookups=removed_lookups)

def main(a):
    if not a or a[0] in ("-h", "--help"): print(__doc__); return 0
    c = db.connect(); kp = db.known_performers(c)
    ids = {int(x) for x in a[a.index("--ids") + 1].split(",")} if "--ids" in a else None
    if a[0] == "report":
        j = junk_entries(c, kp, ids)
        names = {n for v in j.values() for n in v}
        print(f"{len(names)} title-as-person cast entries on {len(j)} scene(s)")
        for fid, v in sorted(j.items()): print(f"  {fid}: {' | '.join(v)}")
    elif a[0] == "plan":
        out = a[1]; cache = {}
        if "--cache" in a:
            with open(a[a.index("--cache") + 1], encoding="utf-8") as f: cache = {int(k): v for k, v in json.load(f).items()}
        token = db.get_setting(c, "tpdb_key", "") if "--tpdb" in a else ""
        j = junk_entries(c, kp, ids)
        rows = [r for r in scene_rows(c, ids) if r["id"] in j]
        plan = []
        for i, r in enumerate(rows):
            cands = cache.get(r["id"])
            if cands is None and token and r["path"]:
                try: cands = tpdb_candidates(token, r["path"])
                except Exception as e: cands = None; print(f"  ThePornDB error for {r['id']}: {type(e).__name__}")
                if cands is not None: cache[r["id"]] = cands
            plan.append(propose(c, r, kp, cands))
            print(f"[{i + 1}/{len(rows)}] {r['id']}: {'OK ' if plan[-1]['confident'] else '?? '}{plan[-1]['new']} - {plan[-1]['why']}", flush=True)
            with open(out, "w", encoding="utf-8") as f: json.dump(plan, f, indent=1, ensure_ascii=False)
            if "--cache-out" in a:
                with open(a[a.index("--cache-out") + 1], "w", encoding="utf-8") as f: json.dump(cache, f, ensure_ascii=False)
        print(f"{sum(p['confident'] for p in plan)} of {len(plan)} scene(s) can be fixed; plan in {out}")
    elif a[0] == "apply":
        with open(a[1], encoding="utf-8") as f: plan = json.load(f)
        r = apply_plan(c, plan)
        print(f"Fixed {len(r['fixed'])} scene(s); skipped (changed or hand-edited) {len(r['skipped'])}; "
              f"removed {r['removed_people']} empty people row(s) and {r['removed_lookups']} lookup row(s)")
        print(json.dumps(r))
    else: print(__doc__); return 2
    return 0

if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
