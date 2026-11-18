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
    p = os.environ.get("FILMTAGGER_REALESRGAN") or os.path.join(data_dir(), "tools", "realesrgan", "realesrgan-ncnn-vulkan.exe")
    return p if os.path.isfile(p) else None

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
