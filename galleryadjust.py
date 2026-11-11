"""Gallery photo adjustments: contrast, brightness, gamma and sharpen.

The current file is overwritten only after a one-time copy of the original is
saved in an `_originals` subfolder next to it (never deleted, never listed in
the gallery). Revert copies that backup back.
"""
import os, shutil, tempfile

ORIGINALS = "_originals"
JPEG_QUALITY = 95
MAX_BYTES = 200 * 1024 * 1024
MAX_PIXELS = 50_000_000
# Neutral defaults: contrast/brightness/gamma 1, sharpen 0 (no unsharp mask).
DEFAULTS = dict(contrast=1.0, brightness=1.0, gamma=1.0, sharpen=0.0)
RANGES = dict(contrast=(0.2, 3.0), brightness=(0.2, 3.0), gamma=(0.2, 3.0), sharpen=(0.0, 3.0))


def original_path(path):
    """Backup path for `path`: <its folder>/_originals/<filename>."""
    folder, name = os.path.split(os.path.abspath(path))
    return os.path.join(folder, ORIGINALS, name)


def has_original(path):
    p = original_path(path)
    return os.path.isfile(p) and os.path.abspath(p) != os.path.abspath(path)


def _hide_originals_dir(folder):
    """Best-effort: mark `_originals` hidden on Windows so Explorer and the gallery skip it."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(str(folder), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    except Exception:
        pass


def ensure_original(path):
    """Copy `path` to `_originals` if no backup exists yet. Returns (backup_path, created)."""
    dest = original_path(path)
    if os.path.isfile(dest):
        return dest, False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _hide_originals_dir(os.path.dirname(dest))
    shutil.copy2(path, dest)
    return dest, True


def parse_params(d):
    """Pull contrast/brightness/gamma/sharpen from a request dict; raise ValueError if out of range."""
    out = {}
    src = d or {}
    for k, (lo, hi) in RANGES.items():
        if k not in src or src[k] in ("", None):
            out[k] = DEFAULTS[k]
            continue
        try:
            v = float(src[k])
        except (TypeError, ValueError) as e:
            raise ValueError(f"Bad {k}") from e
        if v < lo or v > hi or v != v:  # NaN
            raise ValueError(f"{k} must be between {lo} and {hi}")
        out[k] = v
    return out


def is_identity(p):
    return all(abs(p[k] - DEFAULTS[k]) < 1e-6 for k in DEFAULTS)


def _with_rgb(im, fn):
    """Run `fn` on the color image; keep an alpha channel untouched."""
    from PIL import Image
    if im.mode == "RGBA":
        a = im.getchannel("A")
        rgb = fn(im.convert("RGB"))
        return Image.merge("RGBA", rgb.split() + (a,))
    if im.mode == "LA":
        a = im.getchannel("A")
        out = fn(im.convert("L"))
        return Image.merge("LA", (out, a))
    return fn(im)


def apply_gamma(im, gamma):
    """`out = 255 * (in/255) ** (1/gamma)` per channel; gamma 1 is a no-op. Alpha is left alone."""
    from PIL import Image
    if abs(gamma - 1.0) < 1e-6:
        return im
    inv = 1.0 / gamma
    lut = [min(255, max(0, int(round((i / 255.0) ** inv * 255.0)))) for i in range(256)]

    def on(color):
        if color.mode == "L":
            return color.point(lut)
        bands = list(color.split())
        return Image.merge(color.mode, [b.point(lut) for b in bands])

    return _with_rgb(im, on)


def apply_adjustments(im, contrast=1.0, brightness=1.0, gamma=1.0, sharpen=0.0):
    """Apply the four adjustments in this order: contrast, brightness, gamma, sharpen.
    Contrast/brightness use Pillow ImageEnhance; gamma is a LUT; sharpen > 0 is UnsharpMask
    (radius=2, percent=sharpen*100, threshold=3). Neutral values leave the image unchanged."""
    from PIL import ImageEnhance, ImageFilter
    contrast, brightness, gamma, sharpen = float(contrast), float(brightness), float(gamma), float(sharpen)

    def tone(color):
        out = color
        if abs(contrast - 1.0) >= 1e-6:
            out = ImageEnhance.Contrast(out).enhance(contrast)
        if abs(brightness - 1.0) >= 1e-6:
            out = ImageEnhance.Brightness(out).enhance(brightness)
        return out

    im = _with_rgb(im, tone)
    im = apply_gamma(im, gamma)
    if sharpen > 1e-6:
        percent = int(round(sharpen * 100))
        if percent > 0:
            im = _with_rgb(im, lambda c: c.filter(ImageFilter.UnsharpMask(radius=2, percent=percent, threshold=3)))
    return im


def open_image(path):
    """Load an image the way the browser shows it (EXIF orientation applied) and as RGB/RGBA/L."""
    from PIL import Image, ImageOps
    im = Image.open(path)
    im = ImageOps.exif_transpose(im) or im
    im.load()
    if im.mode == "P":
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    elif im.mode == "CMYK":
        im = im.convert("RGB")
    elif im.mode == "LA":
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGB")
    return im


def _save_format(path, im):
    """Format name and a save-ready image for this path's extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        return "JPEG", im, dict(quality=JPEG_QUALITY, optimize=True, subsampling=0)
    if ext == ".png":
        return "PNG", im, dict(optimize=True)
    if ext == ".webp":
        return "WEBP", im, dict(quality=JPEG_QUALITY, method=4)
    if ext == ".bmp":
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        return "BMP", im, {}
    if ext == ".gif":
        from PIL import Image as _Im
        if im.mode not in ("P", "L"):
            im = im.convert("P", palette=_Im.ADAPTIVE)
        return "GIF", im, {}
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    return "JPEG", im, dict(quality=JPEG_QUALITY, optimize=True)


def save_image(im, path):
    """Write `im` over `path`, keeping the original format (JPEG at quality 95). Atomic replace."""
    from PIL import Image as _Image  # noqa: F401
    fmt, im, kw = _save_format(path, im)
    # Orientation is already baked in; don't write a stale Orientation tag.
    if "exif" in kw:
        pass
    fd, tmp = tempfile.mkstemp(prefix=".ga-", suffix=os.path.splitext(path)[1], dir=os.path.dirname(path))
    os.close(fd)
    try:
        im.save(tmp, format=fmt, **kw)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def info(path):
    """Metadata for the editor: size, type, and whether a revert backup exists. Does not open the pixels fully if possible."""
    from PIL import Image, ImageOps
    if not path or not os.path.isfile(path):
        return None, "Photo not found"
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None, "Image is too large (max 200 MB)"
    except OSError:
        return None, "Photo not found"
    try:
        im = Image.open(path)
        im = ImageOps.exif_transpose(im) or im
        w, h = im.size
        ext = (im.format or os.path.splitext(path)[1].lstrip(".")).lower()
        if ext == "jpeg":
            ext = "jpg"
        im.close()
    except Exception as e:
        return None, f"Could not read the photo ({type(e).__name__})"
    return dict(width=w, height=h, ext=ext, has_original=has_original(path),
                defaults=dict(DEFAULTS), ranges={k: {"min": a, "max": b} for k, (a, b) in RANGES.items()}), None


def adjust(path, params):
    """Backup the original once if needed, apply `params`, overwrite `path`. Returns a result dict or (None, error)."""
    if not path or not os.path.isfile(path):
        return None, "Photo not found"
    try:
        size = os.path.getsize(path)
    except OSError:
        return None, "Photo not found"
    if size > MAX_BYTES:
        return None, "Image is too large (max 200 MB)"
    try:
        p = parse_params(params)
    except ValueError as e:
        return None, str(e)
    if is_identity(p):
        return dict(ok=True, unchanged=True, has_original=has_original(path), v=_mtime(path)), None
    try:
        im = open_image(path)
    except Exception as e:
        return None, f"Could not read the photo ({type(e).__name__})"
    if im.size[0] * im.size[1] > MAX_PIXELS:
        return None, "Image has too many pixels"
    try:
        ensure_original(path)
        out = apply_adjustments(im, **p)
        save_image(out, path)
    except Exception as e:
        return None, f"Could not save the photo ({type(e).__name__})"
    return dict(ok=True, unchanged=False, has_original=True, v=_mtime(path), **{k: p[k] for k in DEFAULTS}), None


def revert(path):
    """Replace the current file with the kept original. The backup itself is not deleted."""
    if not path or not os.path.isfile(path):
        return None, "Photo not found"
    src = original_path(path)
    if not os.path.isfile(src):
        return None, "No original is kept for this photo"
    if os.path.abspath(src) == os.path.abspath(path):
        return None, "No original is kept for this photo"
    try:
        shutil.copy2(src, path)
    except Exception as e:
        return None, f"Could not restore the original ({type(e).__name__})"
    return dict(ok=True, reverted=True, has_original=True, v=_mtime(path)), None


def _mtime(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def handle(folder, name, action, body=None):
    """Safe gallery entry point. `action` is info, save or revert. Returns (payload, http_status).
    `name` must be a gallery-safe relative path (see gallery.safe_name); `_originals` is rejected."""
    import gallery as gallerymod
    name = (name or "").replace("\\", "/").strip()
    if not name or name.split("/")[0].lower() == ORIGINALS.lower():
        return dict(error="Photo not found"), 404
    path = gallerymod.safe_name(folder, name)
    if not path:
        return dict(error="Photo not found"), 404
    if action == "info":
        data, err = info(path)
        return (dict(error=err), 400) if err else (data, 200)
    if action == "save":
        data, err = adjust(path, body or {})
        return (dict(error=err), 400) if err else (data, 200)
    if action == "revert":
        data, err = revert(path)
        status = 400 if err else 200
        if err and "No original" in err:
            status = 404
        return (dict(error=err), status) if err else (data, 200)
    return dict(error="Unknown action"), 400
