"""Ask a question: pick a chart for the result shape (and validate the AI's optional 'chart' hint), map chart dimensions
to the main library's filters, and build the 'context' per-year chart. Text/numbers only; the browser draws SVG.
Chart types: ring (part/total/percent), stat (single numbers), series (year/decade/month + numbers; bar or line),
hbar (category ranking, top 20), grouped (category + 2-3 numbers), stacked / heatmap (category x category counts)."""
import re

ROWNUM = {"#", "rank", "rn", "row", "row_number", "rownum", "no", "n#", "pos", "position"}
PCT = re.compile(r"(?i)percent|pct|share|%")
TIME = re.compile(r"(?i)^(year|decade|month|release_year|yr|years?_released|release_decade|release_month)$|(^|_)(year|decade|month)$")
HINTS = {"ring", "donut", "stat", "bar", "line", "hbar", "grouped", "stacked", "heatmap", "table", "none"}
DIM = [  # column name pattern -> library filter
    (re.compile(r"(?i)^(year|release_year|yr)$|_year$"), "year"),
    (re.compile(r"(?i)decade"), "decade"),
    (re.compile(r"(?i)^(month|release_month)$|_month$"), "month"),
    (re.compile(r"(?i)genre"), "pgenre"),
    (re.compile(r"(?i)country|countries"), "country"),
    (re.compile(r"(?i)director"), "director"),
    (re.compile(r"(?i)studio"), "studio"),
    (re.compile(r"(?i)^tags?$|tag_?name|^tag"), "tag"),
    (re.compile(r"(?i)^(performer|person|actor|actress|cast|star)s?$"), "cast"),
]


def _num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)


def dim_of(col):
    for rx, k in DIM:
        if rx.search(col or ""): return k
    return None


def dim_filter(col, v):
    """Main-library filter for one value of a dimension column, or None when it does not map."""
    k = dim_of(col)
    if v is None or v == "" or not k: return None
    if k == "year":
        try: y = int(v)
        except (TypeError, ValueError): return None
        return {"year_min": y, "year_max": y} if 1870 <= y <= 2100 else None
    if k == "decade":
        m = re.match(r"^(\d{4})s?$", str(v).strip())
        return {"decade": m.group(1)} if m else None
    if k == "month":
        try: m = int(v)
        except (TypeError, ValueError): return None
        return {"month": m} if 1 <= m <= 12 else None
    s = str(v).strip()
    if k == "pgenre": return {"pgenre": s}
    if k == "country": return {"country": s}
    if k == "director": return {"person": s, "prole": "director"}
    if k == "studio": return {"studio": s}
    if k == "tag": return {"all": s}
    if k == "cast": return {"person": s, "prole": "cast"}
    return None


def _clauses(sql):
    """(WHERE text, [CASE WHEN conditions]) with string literals kept."""
    s = re.sub(r"--[^\n]*", " ", sql or "")
    cases = re.findall(r"(?is)\bWHEN\b(.*?)\bTHEN\b", s)
    wh = " ".join(m.group(1) for m in re.finditer(r"(?is)\bWHERE\b(.*?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bHAVING\b|\)\s*(?:SELECT|$)|$)", s))
    return wh, cases


def _range(text):
    m = re.search(r"(?i)\byear\s+BETWEEN\s+(\d{4})\s+AND\s+(\d{4})", text)
    if m: return int(m.group(1)), int(m.group(2))
    lo = re.search(r"(?i)\byear\s*(>=|>)\s*(\d{4})", text); hi = re.search(r"(?i)\byear\s*(<=|<)\s*(\d{4})", text)
    if lo and hi: return int(lo.group(2)) + (lo.group(1) == ">"), int(hi.group(2)) - (hi.group(1) == "<")
    m = re.search(r"(?i)\(\s*year\s*/\s*10\s*\)\s*\*\s*10\s*=\s*(\d{4})", text)
    if m: return int(m.group(1)), int(m.group(1)) + 9
    return None


def _dims(text, directors=()):
    f = {}
    m = re.search(r"(?i)primary_genre\s*(?:=|LIKE)\s*'([^'%]+)'", text)
    if m: f["pgenre"] = m.group(1)
    m = re.search(r"(?i)\bcountry\s+LIKE\s+'%?([^'%]+)%?'|\bcountry\s*=\s*'([^']+)'", text)
    if m: f["country"] = m.group(1) or m.group(2)
    m = re.search(r"(?i)\bstudio\s*(?:=|LIKE)\s*'([^'%]+)'", text)
    if m: f["studio"] = m.group(1)
    m = re.search(r"(?i)\b(?:t|tags)\.name\s*(?:=|LIKE)\s*'([^'%]+)'", text)
    if m: f["all"] = m.group(1)
    if re.search(r"(?i)director", text):
        for d in directors:
            if d and d.lower() in text.lower(): f.update(person=d, prole="director"); break
        else:
            m = re.search(r"(?i)director\s*(?:=|LIKE)\s*'([^'%]+)'", text)
            if m: f.update(person=m.group(1), prole="director")
    return f


def query_filters(sql, directors=()):
    """{'base': library filters from the WHERE clause (without the year range), 'range': (y1, y2) from WHERE or
    the percentage's CASE condition, 'part': extra filters of the CASE condition}."""
    wh, cases = _clauses(sql)
    base = _dims(wh, directors)
    rng = _range(wh) or next((r for r in (_range(c) for c in cases) if r), None)
    part = {}
    for c in cases:
        part.update(_dims(c, directors))
        r = _range(c)
        if r: part.update(year_min=r[0], year_max=r[1])
    if _range(wh) is None and rng is None: pass
    return {"base": base, "range": list(rng) if rng else None, "part": part, "range_in_where": _range(wh) is not None}


def _merge(base, f):
    if f is None: return None
    out = dict(base)
    if "person" in f: out.pop("person", None); out.pop("prole", None)
    if "year_min" in f or "decade" in f: out.pop("year_min", None); out.pop("year_max", None); out.pop("decade", None)
    out.update(f)
    return out


def candidates(cols, rows):
    """Chart types that fit the result shape, best first."""
    vk = [k for k, c in enumerate(cols) if c not in ("film_ids", "id") and c.lower() not in ROWNUM]
    if not rows or not vk: return []
    num = [k for k in vk if all(r[k] is None or _num(r[k]) for r in rows) and any(_num(r[k]) for r in rows)]
    tim = [k for k in num if TIME.search(cols[k])] + [k for k in vk if k not in num and TIME.search(cols[k])]
    cat = [k for k in vk if k not in num and k not in tim]
    vals = [k for k in num if k not in tim]
    out = []
    if len(rows) == 1:
        if any(PCT.search(cols[k]) for k in vals): out.append("ring")
        if vals: out.append("stat")
        return out
    if tim and vals: out += ["bar", "line"]
    if len(cat) == 1 and len(vals) == 1: out.append("hbar")
    if len(cat) == 1 and 2 <= len(vals) <= 3: out += ["grouped", "hbar"]
    if len(cat) == 2 and len(vals) >= 1: out += ["stacked", "heatmap"]
    if not out and len(cat) == 1 and vals: out.append("hbar")
    return out


def pick(cols, rows, hint=None, sql="", directors=()):
    """The chart for this result (dict) or None. `hint` is the AI's optional 'chart' choice, used only if it fits."""
    cand = candidates(cols, rows)
    h = str(hint or "").strip().lower(); h = {"donut": "ring", "none": "table", "column": "bar"}.get(h, h)
    if h == "table" and h in HINTS: return None
    kind = h if h in cand else (cand[0] if cand else None)
    if not kind: return None
    qf = query_filters(sql, directors); base = qf["base"]
    vk = [k for k, c in enumerate(cols) if c not in ("film_ids", "id") and c.lower() not in ROWNUM]
    num = [k for k in vk if all(r[k] is None or _num(r[k]) for r in rows) and any(_num(r[k]) for r in rows)]
    tim = [k for k in vk if TIME.search(cols[k])]
    cat = [k for k in vk if k not in num and k not in tim]
    vals = [k for k in num if k not in tim]
    li = cols.index("film_ids") if "film_ids" in cols else None
    ids = lambda r: (str(r[li]) if li is not None and r[li] not in (None, "") else None)
    lib = lambda f: f if f and any(k in f for k in ("person", "pgenre", "country", "studio", "all", "decade", "year_min", "month")) else None
    ch = {"type": kind, "candidates": cand, "hinted": h == kind}
    if kind == "ring":
        r = rows[0]; pk = next(k for k in vals if PCT.search(cols[k])); others = [k for k in vals if k != pk]
        pct = r[pk]
        part = total = None
        if len(others) >= 2:
            a, b = sorted(others[:2], key=lambda k: (r[k] is None, r[k] or 0))
            part, total = r[a], r[b]
        rng = qf["range"]; rl = f"{rng[0]}\u2013{rng[1]}" if rng else "matching"
        ch.update(pct=pct, part=part, total=total, label=rl, other_label="other years" if rng else "other",
                  part_filter=lib(_merge(base, qf["part"])) if qf["part"] else None, total_filter=lib(base) or None, part_ids=ids(r))
        return ch
    if kind == "stat":
        r = rows[0]
        ch["tiles"] = [dict(label=cols[k], value=r[k], pct=bool(PCT.search(cols[k]))) for k in vals[:4]]
        ch["filter"] = lib(base) or None; ch["ids"] = ids(r)
        return ch
    if kind in ("bar", "line"):
        x = tim[0]; ys = [k for k in vals if k != x][:3]
        ch.update(type="series", mode=kind, x=cols[x], series=[cols[k] for k in ys],
                  pct=[bool(PCT.search(cols[k])) for k in ys],
                  items=[dict(label=r[x], values=[r[k] for k in ys], filter=lib(_merge(base, dim_filter(cols[x], r[x]))), ids=ids(r))
                         for r in rows if r[x] is not None],
                  shade=qf["range"] if qf["range"] and not qf["range_in_where"] else None)
        return ch
    if kind == "hbar" or kind == "grouped":
        c = cat[0] if cat else vk[0]; ys = vals[:1] if kind == "hbar" else vals[:3]
        ch.update(type=kind, cat=cols[c], series=[cols[k] for k in ys], pct=[bool(PCT.search(cols[k])) for k in ys],
                  items=[dict(label=r[c], values=[r[k] for k in ys], filter=lib(_merge(base, dim_filter(cols[c], r[c]))), ids=ids(r))
                         for r in rows[:20]], more=max(0, len(rows) - 20))
        return ch
    if kind in ("stacked", "heatmap"):
        a, b = cat[0], cat[1]; v = vals[0]
        A, B = [], []
        for r in rows:
            if r[a] not in A: A.append(r[a])
            if r[b] not in B: B.append(r[b])
        if kind == "stacked" and len(B) > 8: kind = "heatmap"
        A = A[:20]; cell = {}
        for r in rows:
            if r[a] in A: cell[(r[a], r[b])] = (r[v], ids(r), lib(_merge(_merge(base, dim_filter(cols[a], r[a])) or base, dim_filter(cols[b], r[b]))))
        ch.update(type=kind, a=cols[a], b=cols[b], value=cols[v], rows_=A, cols_=B[:24],
                  cells=[[(lambda t: dict(v=t[0], ids=t[1], filter=t[2]) if t else None)(cell.get((x, y))) for y in B[:24]] for x in A])
        return ch
    return None


def context_spec(sql, directors=(), mentions_cast=False):
    """Filters + range for the secondary per-year chart, or None. Needs a non-performer dimension and a year range."""
    qf = query_filters(sql, directors)
    b = qf["base"]
    if not qf["range"] or mentions_cast: return None
    if not any(k in b for k in ("person", "pgenre", "country", "studio", "all")): return None
    if b.get("prole") == "cast": return None
    return {"filter": b, "range": qf["range"]}


def context_title(f):
    if f.get("prole") == "director": return f"{f['person']}'s directed titles by year"
    for k, w in (("pgenre", "{} titles"), ("country", "Titles from {}"), ("studio", "{} scenes"), ("all", "Titles tagged {}")):
        if k in f: return w.format(f[k]) + " by year"
    return "Titles by year"
