"""Smart tagging (AI) bridge - works with any AI chatbot or API.

Typical use: export the queue, paste SMART_TAG_PROMPT.txt + the exported JSON into any AI chat,
save its JSON reply to a file, then import it.

  python smarttag.py export-queue [out.json]     write the smart-tag queue (default: smart_queue.json)
  python smarttag.py import-tags file.json        import AI tags -> pending approval in the app
  python smarttag.py status                       show queue / pending counts
  python smarttag.py clear-queue                  empty the queue

import-tags format: [{"film_id": 12, "tags": [{"name": "heist", "category": "Themes"}, ...]}, ...]
Imported tags get source='ai', status='pending' until approved in the UI. Films are removed from the
queue once their tags are imported (use --keep-queue to keep them).
"""
import sys, json, db

def main(a):
    if not a or a[0] in ("-h", "--help"): print(__doc__); return 0
    c = db.connect(); cmd = a[0]
    if cmd == "export-queue":
        out = a[1] if len(a) > 1 else "smart_queue.json"
        q = db.export_queue(c)
        with open(out, "w", encoding="utf-8") as f: json.dump(q, f, indent=2, ensure_ascii=False)
        print(f"Exported {len(q)} queued film(s) to {out}")
    elif cmd == "import-tags":
        if len(a) < 2: print("usage: smarttag.py import-tags file.json"); return 2
        with open(a[1], encoding="utf-8-sig") as f: data = json.load(f)
        if isinstance(data, dict): data = data.get("items", [data])
        r = db.import_ai_tags(c, data, dequeue="--keep-queue" not in a)
        print(f"Imported {r['tags_added']} pending AI tag(s).", f"Unknown film ids skipped: {r['skipped_film_ids']}" if r["skipped_film_ids"] else "")
    elif cmd == "status":
        print("queue:", c.execute("SELECT COUNT(*) FROM smart_queue").fetchone()[0],
              "| films with pending AI tags:", c.execute("SELECT COUNT(DISTINCT film_id) FROM film_tags WHERE status='pending'").fetchone()[0])
    elif cmd == "clear-queue":
        c.execute("DELETE FROM smart_queue"); c.commit(); print("Queue cleared.")
    else: print(__doc__); return 2
    return 0

if __name__ == "__main__": sys.exit(main(sys.argv[1:]))
