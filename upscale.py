"""Local AI upscaling with Real-ESRGAN (realesrgan-ncnn-vulkan, model realesrgan-x4plus) for the shared image editor.

start(src_path, scale) copies nothing and never touches the source: it runs the tool on a background thread and writes
a PNG preview into <data>/upscale_tmp/<token>.png. The editor shows that preview; Save passes the token so the normal
edit (rotate/flip/crop/tone) runs on the upscaled pixels, with the usual backup of the original.
GPU: the discrete GPU (NVIDIA/AMD RX) is preferred; if it fails the other Vulkan GPU is tried. This build has no CPU mode.
"""
import os, re, subprocess, threading, time, uuid, shutil

MODEL = "realesrgan-x4plus"
TILE = 200            # safe on 4 GB of VRAM (RTX 2050); lower = less memory, a bit slower
MAX_SIDE = 6000       # long side cap of the result (gallery); posters are shrunk to POSTER_FIT_PX later anyway
MAX_IN_PIXELS = 12_000_000
KEEP_SEC = 6 * 3600
JOBS, LOCK, _GPU = {}, threading.Lock(), {}

def data_dir():
    import db
    return os.path.dirname(os.path.abspath(db.DB_PATH))

def exe_path():
    p = os.environ.get("FILMTAGGER_REALESRGAN") or os.path.join(tool_dir(), "realesrgan-ncnn-vulkan.exe")
    return p if os.path.isfile(p) else None

# ---------- first-use download of the official Windows build (pinned asset, size and SHA256) ----------
ASSET_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip"
ASSET_SIZE = 45474481
ASSET_SHA256 = "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d"
ASSET_FILES = {"realesrgan-ncnn-vulkan.exe", "vcomp140.dll", "vcomp140d.dll", "README_windows.md",
               "models/realesrgan-x4plus.bin", "models/realesrgan-x4plus.param"}  # only these are extracted
INSTALL = {"state": "idle"}
IS_WINDOWS = os.name == "nt"
MANUAL_HINT = ("Manual install: download realesrgan-ncnn-vulkan-20220424-windows.zip from "
               "github.com/xinntao/Real-ESRGAN/releases (v0.2.5.0) and unzip it into ")

def tool_dir():
    return os.path.join(data_dir(), "tools", "realesrgan")

def safe_members(names):
    """Zip entry names -> {entry: relative path} for the expected files only; any absolute or '..' entry is refused."""
    out = {}
    for n in names:
        rel = n.replace("\\", "/")
        parts = [x for x in rel.split("/") if x not in ("", ".")]
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in parts: raise ValueError(f"Unsafe path in zip: {n}")
        rel = "/".join(parts)
        if rel in ASSET_FILES: out[n] = rel
    if set(out.values()) != ASSET_FILES: raise ValueError("The download does not contain the expected files")
    return out

def _download(url, dest, job, opener=None):
    import urllib.request, hashlib
    h = hashlib.sha256(); n = 0
    req = urllib.request.Request(url, headers={"User-Agent": "FilmTagger"})
    with (opener or urllib.request.urlopen)(req, timeout=60) as r, open(dest, "wb") as fh:
        while True:
            b = r.read(1 << 16)
            if not b: break
            fh.write(b); h.update(b); n += len(b); job["progress"] = min(99, n * 100 // ASSET_SIZE)
            if n > ASSET_SIZE: raise ValueError("Download is larger than expected")
    return n, h.hexdigest()

def install(job=None, opener=None):
    """Download, verify (size + SHA256), extract only the expected files, check the exe runs. Raises RuntimeError."""
    import zipfile, tempfile
    job = job if job is not None else {}
    if not IS_WINDOWS: raise RuntimeError("Automatic download is for Windows only. " + MANUAL_HINT.replace("windows.zip", "<your OS>.zip") + tool_dir())
    dest = tool_dir(); os.makedirs(os.path.dirname(dest), exist_ok=True)
    work = tempfile.mkdtemp(prefix="realesrgan-", dir=os.path.dirname(dest)); z = os.path.join(work, "dl.zip")
    try:
        try: n, sha = _download(ASSET_URL, z, job, opener)
        except ValueError: raise
        except Exception as e: raise RuntimeError(f"Download failed ({e}). " + MANUAL_HINT + dest)
        if n != ASSET_SIZE or sha != ASSET_SHA256:
            raise RuntimeError("The download did not match the official Real-ESRGAN file (size/SHA256), so it was rejected. " + MANUAL_HINT + dest)
        job["state"] = "extracting"; out = os.path.join(work, "x")
        with zipfile.ZipFile(z) as zf:
            for entry, rel in safe_members(zf.namelist()).items():
                target = os.path.join(out, *rel.split("/")); os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(entry) as src, open(target, "wb") as fh: shutil.copyfileobj(src, fh)
        exe = os.path.join(out, "realesrgan-ncnn-vulkan.exe")
        kw = dict(capture_output=True, text=True, errors="replace", timeout=30, cwd=out)
        if os.name == "nt": kw["creationflags"] = 0x08000000
        r = subprocess.run([exe, "-h"], **kw)
        if "realesrgan-x4plus" not in (r.stdout + r.stderr) and "input-path" not in (r.stdout + r.stderr):
            raise RuntimeError("The downloaded upscaler did not start on this PC. " + MANUAL_HINT + dest)
        if os.path.isdir(dest): shutil.rmtree(dest)
        shutil.move(out, dest)
    except ValueError as e:
        raise RuntimeError(f"{e}. " + MANUAL_HINT + dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return os.path.join(dest, "realesrgan-ncnn-vulkan.exe")

def install_start():
    with LOCK:
        if INSTALL.get("state") in ("downloading", "extracting"): return INSTALL
        INSTALL.clear(); INSTALL.update(state="downloading", progress=0)
    def run():
        try: install(INSTALL); INSTALL.update(state="done", progress=100)
        except Exception as e: INSTALL.update(state="error", error=str(e))
    threading.Thread(target=run, daemon=True).start()
    return INSTALL

def tool_status():
    return dict(installed=bool(exe_path()), windows=IS_WINDOWS, size_mb=round(ASSET_SIZE / 1048576), **INSTALL)

def tmp_dir():
    d = os.path.join(data_dir(), "upscale_tmp"); os.makedirs(d, exist_ok=True); return d

def _gpus(text):
    """'[1 NVIDIA GeForce RTX 2050]  ...' lines -> {index: name}"""
    return {int(m.group(1)): m.group(2).strip() for m in re.finditer(r"^\[(\d+) ([^\]]+)\]", text or "", re.M)}

def _order(gpus):
    """Discrete GPU first, then the rest; [None] (= tool default) when unknown."""
    if not gpus: return [None]
    disc = [i for i, n in gpus.items() if re.search(r"nvidia|geforce|rtx|gtx|radeon rx|arc", n, re.I)]
    return disc + [i for i in sorted(gpus) if i not in disc]

def _run(exe, src, out, gpu, job):
    cmd = [exe, "-i", src, "-o", out, "-n", MODEL, "-s", "4", "-t", str(TILE), "-f", "png"] + (["-g", str(gpu)] if gpu is not None else [])
    kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.path.dirname(exe), text=True, errors="replace")
    if os.name == "nt": kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, **kw); log = []
    for line in p.stdout:
        log.append(line); m = re.match(r"\s*([\d.]+)%", line)
        if m: job["progress"] = min(99, int(float(m.group(1))))
    p.wait()
    return p.returncode, "".join(log)

def _work(token, src, scale):
    job = JOBS[token]
    try:
        from PIL import Image
        exe = exe_path()
        if not exe: raise RuntimeError("Upscaler not installed (realesrgan-ncnn-vulkan.exe missing in Film Tagger Data\\tools\\realesrgan)")
        with Image.open(src) as im:
            w, h = im.size
            if w * h > MAX_IN_PIXELS: raise RuntimeError(f"Image is too large to upscale ({w}×{h})")
            target = min(float(scale), MAX_SIDE / max(w, h))
            if target < 1.05: raise RuntimeError(f"Already {max(w, h)}px on the long side (upscale cap is {MAX_SIDE}px)")
            inp = os.path.join(tmp_dir(), token + "-in.png")
            im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(inp)
        raw = os.path.join(tmp_dir(), token + "-x4.png"); err = ""
        if "list" not in _GPU:  # one tiny probe per run of the app: which Vulkan GPUs exist
            probe = os.path.join(tmp_dir(), token + "-probe.png"); Image.new("RGB", (8, 8)).save(probe)
            _GPU["list"] = _gpus(_run(exe, probe, probe + ".out.png", None, {})[1])
            for q in (probe, probe + ".out.png"):
                try: os.remove(q)
                except OSError: pass
        order = _order(_GPU["list"])
        for n, gpu in enumerate(order):
            job["gpu"] = _GPU["list"].get(gpu, "default GPU")
            code, log = _run(exe, inp, raw, gpu, job)
            if code == 0 and os.path.isfile(raw): break
            err = log.strip().splitlines()[-1] if log.strip() else f"exit {code}"
        else: raise RuntimeError(f"Upscale failed on the GPU ({err}). This build has no CPU mode.")
        if n: job["note"] = f"used {job['gpu']} (the first GPU failed)"
        with Image.open(raw) as im4:
            out = im4.resize((max(1, round(w * target)), max(1, round(h * target))), Image.LANCZOS) if round(w * target) != im4.size[0] else im4.copy()
        dest = os.path.join(tmp_dir(), token + ".png"); out.save(dest)
        with Image.open(inp) as im0:  # plain Lanczos upscale of the original: source of the 'Detail' texture blend
            im0.resize(out.size, Image.LANCZOS).save(os.path.join(tmp_dir(), token + "-lz.png"))
        for p in (inp, raw):
            try: os.remove(p)
            except OSError: pass
        job.update(state="done", progress=100, path=dest, width=out.size[0], height=out.size[1], scale=round(target, 2))
    except Exception as e:
        job.update(state="error", error=str(e))
    finally:
        job["secs"] = round(time.time() - job["started"], 1)

DETAIL_DEFAULT = 50   # % of the original's fine texture (grain, skin) put back on top of the AI result
DETAIL_CLIP = 24      # max per-pixel change from the texture layer: keeps grain, suppresses halos at strong edges

def parse_detail(v):
    try: return max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError): return float(DETAIL_DEFAULT)

def blend(ai, lz, detail, scale):
    """AI result + detail% x clipped high-pass of the Lanczos upscale (lanczos - blur(lanczos)). PIL images, same size."""
    import numpy as np
    from PIL import Image, ImageFilter
    s = parse_detail(detail) / 100.0
    if s <= 0: return ai
    mode = ai.mode; alpha = ai.getchannel("A") if mode == "RGBA" else None
    a = np.asarray(ai.convert("RGB"), dtype=np.float32); l = lz.convert("RGB")
    hp = np.asarray(l, dtype=np.float32) - np.asarray(l.filter(ImageFilter.GaussianBlur(max(1.0, 0.6 * float(scale or 1)))), dtype=np.float32)
    out = Image.fromarray(np.clip(a + s * np.clip(hp, -DETAIL_CLIP, DETAIL_CLIP), 0, 255).astype(np.uint8), "RGB")
    if alpha is not None: out.putalpha(alpha)
    return out

def result_image(token, key, detail=DETAIL_DEFAULT):
    """The finished upscale (made from `key`) with the Detail blend applied, as a PIL image; None if gone/mismatched."""
    from PIL import Image
    p = result(token, key)
    if not p: return None
    lzp = p[:-4] + "-lz.png"
    with Image.open(p) as ai:
        ai.load()
        if not os.path.isfile(lzp): return ai.copy()
        with Image.open(lzp) as lz: return blend(ai, lz, detail, JOBS[token].get("scale"))

def cleanup():
    try:
        for n in os.listdir(tmp_dir()):
            p = os.path.join(tmp_dir(), n)
            if time.time() - os.path.getmtime(p) > KEEP_SEC: os.remove(p)
    except OSError: pass
    for t in [t for t, j in JOBS.items() if time.time() - j["started"] > KEEP_SEC]: JOBS.pop(t, None)

def start(src, scale, key=None):
    """-> token. key ties the result to what it was made from (checked again on Save). Raises ValueError on bad input."""
    if int(scale) not in (2, 4): raise ValueError("Scale must be 2 or 4")
    if not src or not os.path.isfile(src): raise ValueError("Image not found")
    cleanup(); token = uuid.uuid4().hex
    with LOCK: JOBS[token] = dict(state="running", progress=0, key=key or os.path.normcase(os.path.abspath(src)), started=time.time())
    threading.Thread(target=_work, args=(token, src, int(scale)), daemon=True).start()
    return token

def status(token):
    j = JOBS.get(token or "")
    return None if not j else {k: v for k, v in j.items() if k not in ("path", "key")}

def result(token, key):
    """Finished preview path for token, only if it was made from `key` (a file path or e.g. 'poster:12:/posters/x.jpg')."""
    j = JOBS.get(token or "")
    if not j or j.get("state") != "done" or not os.path.isfile(j.get("path", "")): return None
    return j["path"] if key == j["key"] or (os.sep in str(key) and os.path.normcase(os.path.abspath(key)) == j["key"]) else None
