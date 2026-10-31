"""Make stills: ~20 evenly spaced full-resolution JPG frames from a title's video, saved as its gallery
('<video stem> - GALLERY', gallery.canonical_dir) so they show in the normal gallery viewer. A fallback for when
Find Gallery finds nothing. ffmpeg: the install's tools\\ffmpeg folder, else PATH, else a portable build (gyan.dev
essentials) downloaded into tools\\ffmpeg on first use. One job at a time; progress in JOB."""
import os, re, shutil, subprocess, threading, time, zipfile, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools", "ffmpeg")
ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
COUNT, SKIP = 20, 0.05  # frames, and the share of the start/end left out (logos, credits, black)
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

def duration(ffmpeg, video):
    r = subprocess.run([ffmpeg, "-hide_banner", "-i", video], capture_output=True, text=True, errors="replace", creationflags=NOWIN, timeout=120)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else None

def run(fid, video, dest, done=None):
    """Background job body: make COUNT stills of `video` into `dest`."""
    try:
        JOB.update(phase="looking for ffmpeg", done=0, total=COUNT, error=None, dest=dest)
        ff, src = find_ffmpeg()
        if not ff: ff, src = download_ffmpeg()
        JOB.update(source=src, phase="reading video")
        d = duration(ff, video)
        if not d or d < 5: raise RuntimeError("Could not read the video's length")
        os.makedirs(dest, exist_ok=True); JOB["phase"] = "making stills"
        for i in range(COUNT):
            t = d * (SKIP + (1 - 2 * SKIP) * i / (COUNT - 1))
            hh, rem = divmod(int(t), 3600); mm, ss = divmod(rem, 60)
            out = os.path.join(dest, f"still {i + 1:02d} ({hh:02d}h{mm:02d}m{ss:02d}s).jpg")
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-q:v", "2", out],
                           capture_output=True, creationflags=NOWIN, timeout=300)
            JOB["done"] = i + 1
        if not any(n.startswith("still ") for n in os.listdir(dest)): raise RuntimeError("ffmpeg made no stills")
        JOB["phase"] = "done"
    except Exception as e:
        JOB.update(error=str(e) or type(e).__name__, phase="failed")
    finally:
        JOB.update(running=False, finished_at=time.time())
        if done:
            try: done()
            except Exception: pass

def start(fid, video, dest, done=None):
    JOB.update(running=True, film_id=fid, started_at=time.time(), source=None)
    threading.Thread(target=run, args=(fid, video, dest, done), daemon=True).start()
