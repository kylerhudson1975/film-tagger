"""Make stills: 10, 20 (default) or 40 evenly spaced, sharp (never motion-blurred) full-resolution JPG frames from a title's video, saved as its gallery
('<video stem> - GALLERY', gallery.canonical_dir) so they show in the normal gallery viewer. A fallback for when
Find Gallery finds nothing. ffmpeg: the install's tools\\ffmpeg folder, else PATH, else a portable build (gyan.dev
essentials) downloaded into tools\\ffmpeg on first use. One job at a time; progress in JOB."""
import os, re, shutil, subprocess, threading, time, zipfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools", "ffmpeg")
ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
COUNT, SKIP = 20, 0.05
COUNTS = (10, 20, 40)  # the choices offered by the Make stills popup  # frames, and the share of the start/end left out (logos, credits, black)
JOB = dict(running=False, film_id=None, phase=None, done=0, total=0, error=None, dest=None, source=None, dl_bytes=0, dl_total=None)
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def find_ffmpeg():
    """-> (path, source) or (None, None)."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for root, _, files in os.walk(os.path.join(HERE, "tools")) if os.path.isdir(os.path.join(HERE, "tools")) else []:
        if exe in files: return os.path.join(root, exe), "install tools folder"
    p = shutil.which("ffmpeg")
    return (p, "PATH") if p else (None, None)

def download_ffmpeg():
    """Portable ffmpeg into tools\\ffmpeg (ffmpeg.exe + ffprobe.exe only). Windows only."""
    if os.name != "nt": raise RuntimeError("ffmpeg not found on PATH")
    os.makedirs(TOOLS, exist_ok=True); tmp = os.path.join(TOOLS, "ffmpeg-download.zip")
    JOB.update(phase="downloading ffmpeg", dl_bytes=0, dl_total=None)
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "FilmTagger"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as fh:
        JOB["dl_total"] = int(r.headers.get("Content-Length") or 0) or None
        while True:
            b = r.read(1 << 20)
            if not b: break
            fh.write(b); JOB["dl_bytes"] += len(b)
    JOB["phase"] = "unpacking ffmpeg"
    with zipfile.ZipFile(tmp) as z:
        for n in z.namelist():
            if n.lower().endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                with z.open(n) as src, open(os.path.join(TOOLS, os.path.basename(n)), "wb") as dst: shutil.copyfileobj(src, dst)
    os.remove(tmp)
    p = os.path.join(TOOLS, "ffmpeg.exe")
    if not os.path.isfile(p): raise RuntimeError("ffmpeg.exe not found in the download")
    return p, "downloaded (gyan.dev essentials)"

def probe(ffmpeg, video):
    """-> (seconds, width, height, fps) from ffmpeg's banner; any part may be None."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", video], capture_output=True, text=True, errors="replace", creationflags=NOWIN, timeout=120)
    e = r.stderr or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", e)
    d = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None
    v = re.search(r"Stream #[^\n]*Video:[^\n]*?\b(\d{2,5})x(\d{2,5})\b", e)
    f = re.search(r"Stream #[^\n]*Video:[^\n]*?([\d.]+) (?:fps|tbr)", e)
    return d, (int(v.group(1)) if v else None), (int(v.group(2)) if v else None), (float(f.group(1)) if f else None)

def duration(ffmpeg, video):
    return probe(ffmpeg, video)[0]

# Sharp stills: around each spot, CANDS frames over WIN seconds are decoded small (SW px wide, grey) and scored; the
# sharpest usable one is then saved at full resolution. Frames are only ever handled as numbers here.
WIN, CANDS, SW = 3.0, 10, 480
DARK, BRIGHT, FLAT = 20, 238, 14  # mean-brightness limits and minimum contrast (std): black, white, fades, flat frames
REL = 0.5  # a spot's best must reach this share of the film's typical best score, or the search widens / moves

def sharpness(g):
    """g: 2-D grey array. -> score, or None for a near-black / near-white / flat (fade) frame. The score is the
    weakest of four directional gradient energies, scaled by the Laplacian's: motion blur wipes out detail along
    the motion's direction, so a smeared frame scores low even if it keeps edges across the motion."""
    import numpy as np
    g = g.astype(np.float32); m, sd = float(g.mean()), float(g.std())
    if m < DARK or m > BRIGHT or sd < FLAT: return None
    g = g[2:-2, 2:-2]
    d = [np.mean(np.square(g[:, 1:] - g[:, :-1])), np.mean(np.square(g[1:, :] - g[:-1, :])),
         np.mean(np.square(g[1:, 1:] - g[:-1, :-1])) / 2, np.mean(np.square(g[1:, :-1] - g[:-1, 1:])) / 2]
    lap = g[1:-1, :-2] + g[1:-1, 2:] + g[:-2, 1:-1] + g[2:, 1:-1] - 4 * g[1:-1, 1:-1]
    return float(np.sqrt(min(d) * float(lap.var())))

def candidates(ff, video, t0, win, n, fps, sw, sh):
    """Score n frames spread over [t0, t0+win]. -> list of (score or None, frame index after the seek)."""
    import numpy as np
    step = max(1, int(round(fps * win / n)))
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-ss", f"{t0:.3f}", "-i", video, "-an", "-sn",
                        "-vf", f"select='not(mod(n\\,{step}))',scale={sw}:{sh}:flags=area,format=gray", "-fps_mode", "passthrough",
                        "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
                       capture_output=True, creationflags=NOWIN, timeout=300)
    a, sz = r.stdout or b"", sw * sh
    return [(sharpness(np.frombuffer(a[k * sz:(k + 1) * sz], np.uint8).reshape(sh, sw)), k * step) for k in range(len(a) // sz)]

def save_frame(ff, video, t0, idx, out):
    """Full-resolution JPG of frame `idx` after the same seek used for scoring (so it is the very frame scored)."""
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t0:.3f}", "-i", video, "-an", "-sn",
                    "-vf", f"select='eq(n\\,{idx})'", "-fps_mode", "passthrough", "-frames:v", "1", "-q:v", "2", out],
                   capture_output=True, creationflags=NOWIN, timeout=300)
    return os.path.isfile(out) and os.path.getsize(out) > 0

def best_at(ff, video, d, t, win, n, fps, sw, sh):
    """Sharpest usable candidate in a window centred on t. -> (score, t0, idx) or None."""
    t0 = min(max(0.0, t - win / 2), max(0.0, d - win - 1))
    c = [(s, i) for s, i in candidates(ff, video, t0, win, n, fps, sw, sh) if s is not None]
    if not c: return None
    s, i = max(c)
    return s, t0, i

PAT = re.compile(r"still (\d{2,3}) \(\d{2}h\d{2}m\d{2}s\)\.jpg$")

def next_number(dest):
    """First free still number in dest (Add to existing: new stills follow the old ones, nothing is overwritten)."""
    try: ns = [int(m.group(1)) for m in map(PAT.match, os.listdir(dest)) if m]
    except OSError: ns = []
    return max(ns, default=0) + 1

def backup_existing(dest, img_ext):
    """Replace: move the folder's current images and shoot subfolders into a hidden '.stills-backup-<time>' subfolder
    (hidden so the gallery no longer shows them). Never deletes. -> (backup folder name, items moved)."""
    if not os.path.isdir(dest): return None, 0
    name = ".stills-backup-" + time.strftime("%Y%m%d-%H%M%S"); bk = os.path.join(dest, name); k = 2
    while os.path.exists(bk): bk = os.path.join(dest, f"{name}-{k}"); k += 1
    moved = 0
    for n in sorted(os.listdir(dest)):
        p = os.path.join(dest, n)
        if n.startswith((".", "$", "~")): continue
        if (os.path.isfile(p) and os.path.splitext(n)[1].lower() in img_ext) or (os.path.isdir(p) and not os.path.islink(p)):
            os.makedirs(bk, exist_ok=True); shutil.move(p, os.path.join(bk, n)); moved += 1
    return (os.path.basename(bk) if moved else None), moved

def run(fid, video, dest, done=None, count=COUNT, start_no=1):
    """Background job body: make up to `count` sharp stills of `video` into `dest`, numbered from start_no."""
    COUNT = count if count in COUNTS else globals()["COUNT"]
    try:
        JOB.update(phase="looking for ffmpeg", done=0, total=COUNT, error=None, dest=dest, skipped=0)
        ff, src = find_ffmpeg()
        if not ff: ff, src = download_ffmpeg()
        JOB.update(source=src, phase="reading video")
        d, w, h, fps = probe(ff, video)
        if not d or d < 5: raise RuntimeError("Could not read the video's length")
        fps = fps if fps and 5 <= fps <= 120 else 24.0
        sw = SW if not w or w >= SW else w - w % 2
        sh = max(2, int(round(sw * h / w / 2)) * 2) if w and h else (sw * 9 // 16) // 2 * 2
        spots = [d * (SKIP + (1 - 2 * SKIP) * i / (COUNT - 1)) for i in range(COUNT)]
        gap = d * (1 - 2 * SKIP) / (COUNT - 1)
        JOB["phase"] = "finding sharp frames"; picks = []
        for i, t in enumerate(spots):
            picks.append(best_at(ff, video, d, t, WIN, CANDS, fps, sw, sh)); JOB["done"] = i + 1
        good = sorted(p[0] for p in picks if p)
        if not good: raise RuntimeError("No usable frames found (all dark or blank?)")
        floor = REL * good[len(good) // 2]  # relative to this film's own typical best frame
        JOB.update(phase="making stills", done=0)
        os.makedirs(dest, exist_ok=True); made = []
        for i, t in enumerate(spots):
            p = picks[i]
            if not p or p[0] < floor:  # widen the window, then try spots a little before / after
                for tt, win, n in ((t, 3 * WIN, 3 * CANDS), (t - gap / 3, 2 * WIN, 2 * CANDS), (t + gap / 3, 2 * WIN, 2 * CANDS),
                                   (t - gap / 2 + WIN, 2 * WIN, 2 * CANDS), (t + gap / 2 - WIN, 2 * WIN, 2 * CANDS)):
                    q = best_at(ff, video, d, tt, win, n, fps, sw, sh)
                    if q and (not p or q[0] > p[0]): p = q
                    if p and p[0] >= floor: break
            if p and p[0] >= floor:
                at = p[1] + p[2] / fps; hh, rem = divmod(int(at), 3600); mm, ss = divmod(rem, 60)
                out = os.path.join(dest, f"still {start_no + len(made):02d} ({hh:02d}h{mm:02d}m{ss:02d}s).jpg")
                if save_frame(ff, video, p[1], p[2], out): made.append(os.path.basename(out))
            else: JOB["skipped"] += 1
            JOB["done"] = len(made)
        if not made: raise RuntimeError("ffmpeg made no stills")
        JOB.update(phase="done", saved=len(made))
    except Exception as e:
        JOB.update(error=str(e) or type(e).__name__, phase="failed")
    finally:
        JOB.update(running=False, finished_at=time.time())
        if done:
            try: done()
            except Exception: pass

def start(fid, video, dest, done=None, count=COUNT, start_no=1):
    JOB.update(running=True, film_id=fid, started_at=time.time(), source=None, saved=0, done=0, error=None, phase="starting")
    threading.Thread(target=run, args=(fid, video, dest, done, count, start_no), daemon=True).start()
