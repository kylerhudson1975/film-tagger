"""Subtitle detection for Film Tagger.

External files next to the video (.srt .ass .ssa .vtt .sub/.idx, also in a Subs/Subtitles subfolder) with the
language parsed from the name (Title.en.srt, Title.eng.forced.srt, 2_English.srt; plain Title.srt = Unknown), plus
embedded subtitle streams in MKV/MP4/MOV/WebM via ffprobe (built-in MKV/MP4 header reader when ffprobe is missing).
Embedded probes are cached in the sub_probe table by path + size + mtime so rescans never re-probe unchanged files.
Result text looks like 'English (SRT); French (embedded)' or 'None'. Never raises."""
import os, re, json, struct, subprocess
import resolution as resmod

SUB_EXT = {".srt": "SRT", ".ass": "ASS", ".ssa": "SSA", ".vtt": "VTT", ".sub": "SUB", ".idx": "VobSub"}
EMBED_EXT = {".mkv", ".mp4", ".m4v", ".mov", ".webm", ".mk3d"}
SUB_DIRS = {"subs", "sub", "subtitles", "subtitle"}
NONE = "None"

LANGS = {
    "en": "English", "eng": "English", "english": "English", "fr": "French", "fre": "French", "fra": "French", "french": "French",
    "de": "German", "ger": "German", "deu": "German", "german": "German", "es": "Spanish", "spa": "Spanish", "spanish": "Spanish",
    "esp": "Spanish", "it": "Italian", "ita": "Italian", "italian": "Italian", "pt": "Portuguese", "por": "Portuguese",
    "portuguese": "Portuguese", "pb": "Portuguese (BR)", "pob": "Portuguese (BR)", "ptbr": "Portuguese (BR)", "nl": "Dutch",
    "dut": "Dutch", "nld": "Dutch", "dutch": "Dutch", "sv": "Swedish", "swe": "Swedish", "swedish": "Swedish", "da": "Danish",
    "dan": "Danish", "danish": "Danish", "no": "Norwegian", "nor": "Norwegian", "nob": "Norwegian", "nb": "Norwegian",
    "norwegian": "Norwegian", "fi": "Finnish", "fin": "Finnish", "finnish": "Finnish", "pl": "Polish", "pol": "Polish",
    "polish": "Polish", "cs": "Czech", "cze": "Czech", "ces": "Czech", "czech": "Czech", "hu": "Hungarian", "hun": "Hungarian",
    "hungarian": "Hungarian", "ro": "Romanian", "rum": "Romanian", "ron": "Romanian", "romanian": "Romanian", "el": "Greek",
    "gre": "Greek", "ell": "Greek", "greek": "Greek", "tr": "Turkish", "tur": "Turkish", "turkish": "Turkish", "ru": "Russian",
    "rus": "Russian", "russian": "Russian", "uk": "Ukrainian", "ukr": "Ukrainian", "ja": "Japanese", "jpn": "Japanese",
    "japanese": "Japanese", "ko": "Korean", "kor": "Korean", "korean": "Korean", "zh": "Chinese", "chi": "Chinese",
    "zho": "Chinese", "chinese": "Chinese", "chs": "Chinese", "cht": "Chinese", "ar": "Arabic", "ara": "Arabic",
    "arabic": "Arabic", "he": "Hebrew", "heb": "Hebrew", "hebrew": "Hebrew", "hi": "Hindi", "hin": "Hindi", "th": "Thai",
    "tha": "Thai", "vi": "Vietnamese", "vie": "Vietnamese", "id": "Indonesian", "ind": "Indonesian", "hr": "Croatian",
    "hrv": "Croatian", "sr": "Serbian", "srp": "Serbian", "sl": "Slovenian", "slv": "Slovenian", "sk": "Slovak", "slo": "Slovak",
    "slk": "Slovak", "bg": "Bulgarian", "bul": "Bulgarian", "is": "Icelandic", "ice": "Icelandic", "isl": "Icelandic",
    "ca": "Catalan", "cat": "Catalan", "et": "Estonian", "est": "Estonian", "lv": "Latvian", "lav": "Latvian",
    "lt": "Lithuanian", "lit": "Lithuanian", "fa": "Persian", "per": "Persian", "fas": "Persian", "ms": "Malay", "may": "Malay",
}
FLAGS = {"forced", "sdh", "cc", "hi", "default", "full", "signs"}

def lang_name(code):
    """'en' / 'eng' / 'English' / 'en-US' -> 'English'; unknown/empty -> None."""
    k = re.sub(r"[^a-z]", "", (code or "").lower())
    if not k or k in ("und", "unk", "zxx", "mis", "mul"): return None
    if k in LANGS: return LANGS[k]
    k2 = re.split(r"[-_]", (code or "").lower())[0]
    return LANGS.get(k2)

def lang_from_name(sub_stem, video_stem=None):
    """Language from a subtitle file stem. Looks at the tokens after the video's stem (or every dotted/underscored
    token) for a language code/name. Returns e.g. 'English' or 'Unknown'."""
    s = sub_stem
    if video_stem and s.lower().startswith(video_stem.lower()): s = s[len(video_stem):]
    toks = [t for t in re.split(r"[.\s_\-\[\]()]+", s) if t]
    toks = [t for t in toks if t.lower() not in FLAGS]
    if toks:  # only the last token counts, so title words ('It', 'No') are never read as a language
        n = LANGS.get(toks[-1].lower())
        if n: return n
    return "Unknown"

def _listdir(d, cache):
    if cache is not None and d in cache: return cache[d]
    try: names = os.listdir(d)
    except OSError: names = []
    if cache is not None: cache[d] = names
    return names

def external(video, dircache=None):
    """[(language, FORMAT)] for subtitle files belonging to `video`."""
    d, fn = os.path.split(video); stem = os.path.splitext(fn)[0]; low = stem.lower()
    names = _listdir(d, dircache)
    vids = [n for n in names if os.path.splitext(n)[1].lower() in resmod_video_ext()]
    only = len(vids) <= 1
    out, idx_stems = [], set()
    cands = [(d, n, True) for n in names]
    for n in names:
        if n.lower() in SUB_DIRS and os.path.isdir(os.path.join(d, n)):
            cands += [(os.path.join(d, n), m, False) for m in _listdir(os.path.join(d, n), dircache)]
    subs = [(dd, n, top) for dd, n, top in cands if os.path.splitext(n)[1].lower() in SUB_EXT]
    for dd, n, top in subs:
        if os.path.splitext(n)[1].lower() == ".idx": idx_stems.add((dd, os.path.splitext(n)[0].lower()))
    for dd, n, top in subs:
        st, ext = os.path.splitext(n); ext = ext.lower()
        if not (st.lower().startswith(low) or only): continue
        if ext == ".sub" and (dd, st.lower()) in idx_stems: continue  # VobSub pair counts once (as .idx)
        langs = _idx_langs(os.path.join(dd, n)) if ext == ".idx" else []
        if not langs: langs = [lang_from_name(st, stem if st.lower().startswith(low) else None)]
        for lg in langs: out.append((lg, SUB_EXT[ext]))
    return out

def resmod_video_ext():
    import db
    return db.VIDEO_EXT

def _idx_langs(p):
    try:
        with open(p, "r", errors="ignore") as f: txt = f.read(200000)
    except OSError: return []
    return list(dict.fromkeys(lang_name(m) or "Unknown" for m in re.findall(r"^id:\s*([A-Za-z]{2,3})", txt, re.M)))

# ---------- embedded ----------
def probe_ffprobe(path):
    ff = resmod.ffprobe_path()
    if not ff: return None
    try:
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
        r = subprocess.run([ff, "-v", "error", "-select_streams", "s", "-show_entries", "stream=codec_name:stream_tags=language,title",
                            "-of", "json", path], capture_output=True, timeout=60, **kw)
        if r.returncode != 0 and not r.stdout: return None
        out = []
        for s in json.loads(r.stdout or b"{}").get("streams") or []:
            t = s.get("tags") or {}
            out.append(dict(lang=t.get("language") or t.get("LANGUAGE"), title=t.get("title"), codec=s.get("codec_name")))
        return out
    except Exception:
        return None

def _mkv_subs(f, fsize):
    f.seek(0); v = resmod._vint
    eid, _ = v(f, True)
    if eid != 0x1A45DFA3: return None
    sz, _ = v(f); f.seek(f.tell() + sz)
    eid, _ = v(f, True)
    if eid != 0x18538067: return None
    ssz, _ = v(f); pos = f.tell(); end = fsize if ssz < 0 else min(fsize, pos + ssz)
    def children(p, e, limit=200000):
        n = 0
        while p < e and n < limit:
            f.seek(p); i, _ = v(f, True); s, _ = v(f)
            if i is None or s is None: return
            d = f.tell(); yield i, d, (e if s < 0 else d + s); n += 1
            if s < 0: return
            p = d + s
    for i, d, e in children(pos, end):
        if i == 0x1654AE6B:
            out = []
            for ti, td, te in children(d, e):
                if ti != 0xAE: continue
                typ, lang, codec = None, "eng", None
                for pi, pd, pe in children(td, te):
                    f.seek(pd); raw = f.read(min(pe - pd, 256))
                    if pi == 0x83: typ = int.from_bytes(raw, "big")
                    elif pi == 0x22B59C: lang = raw.rstrip(b"\0").decode("ascii", "ignore")
                    elif pi == 0x22B59D: lang = raw.rstrip(b"\0").decode("ascii", "ignore")
                    elif pi == 0x86: codec = raw.rstrip(b"\0").decode("ascii", "ignore")
                if typ == 0x11: out.append(dict(lang=lang, title=None, codec=codec))
            return out
        if i == 0x1F43B675: break  # reached clusters without Tracks
    return []

def _mp4_subs(f, fsize):
    out = []
    for typ, s, e in resmod._boxes(f, 0, fsize):
        if typ != b"moov": continue
        for t2, s2, e2 in resmod._boxes(f, s, e):
            if t2 != b"trak": continue
            for t3, s3, e3 in resmod._boxes(f, s2, e2):
                if t3 != b"mdia": continue
                hd, lang = None, None
                for t4, s4, e4 in resmod._boxes(f, s3, e3):
                    if t4 == b"hdlr": f.seek(s4 + 8); hd = f.read(4)
                    elif t4 == b"mdhd":
                        f.seek(s4); ver = f.read(1)[0]
                        f.seek(s4 + (20 if ver == 0 else 32)); code = struct.unpack(">H", f.read(2))[0]
                        lang = "".join(chr(((code >> sh) & 31) + 0x60) for sh in (10, 5, 0))
                if hd in (b"sbtl", b"subt", b"text", b"clcp"): out.append(dict(lang=lang, title=None, codec=hd.decode()))
        return out
    return None

def probe_builtin(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        fsize = os.path.getsize(path)
        with open(path, "rb") as f:
            for r in ((_mkv_subs, _mp4_subs) if ext in (".mkv", ".webm", ".mk3d") else (_mp4_subs, _mkv_subs)):
                try:
                    x = r(f, fsize)
                    if x is not None: return x
                except Exception: continue
    except OSError: pass
    return None

def embedded(c, path):
    """[(language, 'embedded')] using the sub_probe cache keyed by (size, mtime)."""
    if os.path.splitext(path)[1].lower() not in EMBED_EXT: return []
    try: st = os.stat(path)
    except OSError: return []
    size, mtime = st.st_size, int(st.st_mtime)
    r = c.execute("SELECT size, mtime, streams FROM sub_probe WHERE path=?", (path,)).fetchone() if c is not None else None
    if r and r[0] == size and r[1] == mtime and r[2] is not None:
        streams = json.loads(r[2])
    else:
        streams, how = probe_ffprobe(path), "ffprobe"
        if streams is None: streams, how = probe_builtin(path), "header"
        if streams is None: streams = []
        if c is not None:
            c.execute("INSERT OR REPLACE INTO sub_probe(path,size,mtime,streams) VALUES(?,?,?,?)", (path, size, mtime, json.dumps(streams)))
    return [((lang_name(s.get("lang")) or lang_name(s.get("title")) or "Unknown"), "embedded") for s in streams]

def fmt(items):
    seen = []
    for lg, kind in items:
        if (lg, kind) not in seen: seen.append((lg, kind))
    if not seen: return NONE
    # group formats per language: 'English (SRT, embedded)'
    order, m = [], {}
    for lg, kind in seen:
        if lg not in m: order.append(lg); m[lg] = []
        m[lg].append(kind)
    order.sort(key=lambda x: (x == "Unknown", x != "English", x))
    return "; ".join(f"{lg} ({', '.join(m[lg])})" for lg in order)

def detect(c, paths, dircache=None):
    """Subtitle text for a film's files, or None if every file is on an unmounted drive (retry later)."""
    any_mounted, items = False, []
    for p in paths:
        if not resmod.is_mounted(p): continue
        any_mounted = True
        if not os.path.isfile(p): continue
        items += external(p, dircache)
        items += embedded(c, p)
    if paths and not any_mounted: return None
    return fmt(items)

def has_subs(v):
    return bool(v) and v.strip() not in ("", NONE)

def languages(v):
    """'English (SRT); French (embedded)' -> ['English', 'French']."""
    if not has_subs(v): return []
    return [re.sub(r"\s*\(.*$", "", x).strip() for x in v.split(";") if x.strip()]

def lang_in(v, lang):
    return 1 if lang and any(x.casefold() == lang.casefold() for x in languages(v)) else 0

def method_name():
    return "ffprobe" if resmod.ffprobe_path() else "built-in MKV/MP4 reader"
