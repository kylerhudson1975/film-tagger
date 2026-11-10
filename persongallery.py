"""Performer photo galleries on Person Profiles.

Each person may have one gallery folder (people.gallery_dir), linked in place: Film Tagger only lists and reads the
images (thumbnails are cached under the app data folder); files are never moved, renamed or changed. Images added to
the folder later show up automatically because the folder is listed on every visit.

match_folders() pairs a root's subfolder names (one folder per performer, e.g. 'Porn Stars\\Crystal Clark') with
people by name or alias, case/accent/whitespace-insensitively. Only unambiguous matches are linked automatically."""
import os, re, json
import db, gallery as gallerymod

def key(s):
    """'  Crystal   CLARK ' / 'Crystál_Clark' -> 'crystal clark'."""
    s = re.sub(r"[_.]+", " ", s or "")
    return " ".join(db.fold(s).split())

def match_folders(folders, people):
    """folders: [name]; people: [{id, name, aliases: [str], adult: bool}] (merged people already left out).
    -> [{folder, kind: 'exact'|'alias'|'ambiguous'|'none', person_id, candidates: [{id, name, via}]}].
    A folder matching several people is narrowed to the adult performers among them; still more than one -> ambiguous.
    Aliases count only with two or more words (as everywhere else in Film Tagger: 'Andi' alone would match strangers)."""
    by_name, by_alias = {}, {}
    for p in people:
        k = key(p.get("name"))
        if k: by_name.setdefault(k, {})[p["id"]] = p
        for a in p.get("aliases") or []:
            ka = key(a)
            if ka and len(ka.split()) >= 2 and ka != k: by_alias.setdefault(ka, {})[p["id"]] = p
    out = []
    for f in folders:
        k = key(f)
        hits = {}
        for pid, p in by_name.get(k, {}).items(): hits[pid] = (p, "name")
        for pid, p in by_alias.get(k, {}).items(): hits.setdefault(pid, (p, "alias"))
        if len(hits) > 1:
            adult = {pid: h for pid, h in hits.items() if h[0].get("adult")}
            if adult: hits = adult
        cands = sorted((dict(id=pid, name=p["name"], via=via) for pid, (p, via) in hits.items()), key=lambda x: (x["name"].lower(), x["id"]))
        if not hits: kind = "none"
        elif len(hits) > 1: kind = "ambiguous"
        else: kind = "exact" if cands[0]["via"] == "name" else "alias"
        out.append(dict(folder=f, kind=kind, person_id=cands[0]["id"] if len(hits) == 1 else None, candidates=cands))
    return out

def survey(root):
    """Performer subfolders of `root` (hidden ones skipped) with image counts, plus loose image files at the top level."""
    subs = gallerymod.subfolders(root)
    return ([dict(folder=n, path=os.path.join(root, n), count=len(gallerymod.images(os.path.join(root, n)))) for n in subs],
            gallerymod._imgs_in(root))

def people_for_match(c, adult_ids):
    """Every unmerged person with an adult flag (ThePornDB performer, or credited in an adult title)."""
    cast, cast_names = set(), {}
    if adult_ids:
        ids = list(adult_ids)
        for i in range(0, len(ids), 900):
            for (v,) in c.execute(f"SELECT cast_ FROM films WHERE id IN ({','.join('?' * len(ids[i:i+900]))})", ids[i:i+900]):
                for n in (v or "").split(","):
                    k = key(n)
                    if k: cast.add(k); cast_names.setdefault(k, " ".join(n.split()))
    out, known = [], set()
    for r in c.execute("SELECT id, name, aliases, tpdb_performer_id FROM people WHERE merged_into IS NULL"):
        try: al = json.loads(r["aliases"] or "[]")
        except Exception: al = []
        names = [r["name"]] + [a for a in al if isinstance(a, str)]
        adult = (r["tpdb_performer_id"] not in (None, "", "-")) or any(key(n) in cast for n in names)
        out.append(dict(id=r["id"], name=r["name"], aliases=[a for a in al if isinstance(a, str)], adult=adult))
        known.update(key(n) for n in names)
    for k, n in sorted(cast_names.items()):  # credited performers without a profile row yet: linking creates the row
        if k not in known and len(k.split()) >= 2: out.append(dict(id="new:" + k, name=n, aliases=[], adult=True))
    return out
