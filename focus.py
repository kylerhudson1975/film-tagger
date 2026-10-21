"""Smart poster cropping for wide (landscape) scene covers.

Computes a horizontal focus for showing a wide image in a 2:3 poster box: the value is the CSS
object-position X fraction (0.0 = left edge, 0.5 = centre, 1.0 = right edge), so it is always inside the image.
Order: YuNet face detector (models/face_detection_yunet_2023mar.onnx, MIT) -> OpenCV HOG person detector
-> edge-energy window (where the detail is) -> centre. Images are decoded in memory only; nothing is written."""
import os

ASPECT = 2 / 3
MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "face_detection_yunet_2023mar.onnx")
_cv = None
_hog = None

def available():
    global _cv
    if _cv is None:
        try:
            import cv2, numpy  # noqa: F401
            _cv = cv2
        except Exception:
            _cv = False
    return bool(_cv)

def _span_focus(cx, w, cw):
    """Crop centre x (pixels) -> object-position fraction, clamped so the crop stays inside the image."""
    if w <= cw: return 0.5
    left = min(max(cx - cw / 2, 0), w - cw)
    return round(left / (w - cw), 4)

def _pick(boxes, cw):
    """boxes: [(x, y, w, h, weight)]. Union of all; if wider than the crop, add largest first while they fit."""
    if not boxes: return None, 0
    bs = sorted(boxes, key=lambda b: -b[4])
    lo, hi = bs[0][0], bs[0][0] + bs[0][2]; used = 1
    for b in bs[1:]:
        nlo, nhi = min(lo, b[0]), max(hi, b[0] + b[2])
        if nhi - nlo <= cw: lo, hi, used = nlo, nhi, used + 1
    return (lo + hi) / 2, used

def _faces(img):
    cv2 = _cv
    if not os.path.isfile(MODEL) or not hasattr(cv2, "FaceDetectorYN"): return []
    h, w = img.shape[:2]
    det = cv2.FaceDetectorYN.create(MODEL, "", (w, h), 0.7, 0.3, 50)
    det.setInputSize((w, h))
    _, f = det.detect(img)
    out = []
    if f is not None:
        for r in f:
            x, y, bw, bh, score = float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[-1])
            if bh >= h * 0.04 and bw > 4: out.append((x, y, bw, bh, bw * bh * score, score))
    return out

def _people(img):
    global _hog
    cv2 = _cv
    if _hog is None:
        _hog = cv2.HOGDescriptor(); _hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    rects, weights = _hog.detectMultiScale(g, winStride=(8, 8), padding=(8, 8), scale=1.08)
    out = []
    for (x, y, bw, bh), wt in zip(rects, list(weights.ravel()) if len(rects) else []):
        if wt > 0.5 and bh >= img.shape[0] * 0.25: out.append((float(x), float(y), float(bw), float(bh), float(bw * bh * wt)))
    return out

def _energy(img, cw):
    cv2, np = _cv, __import__("numpy")
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype("float32")
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3); gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    col = np.sqrt(gx * gx + gy * gy).sum(axis=0)
    w = col.shape[0]; k = max(1, min(w, int(round(cw))))
    cs = np.concatenate([[0.0], np.cumsum(col)]); win = cs[k:] - cs[:-k]
    if win.size == 0 or win.max() <= 0: return None
    if (win.max() - win.min()) / win.max() < 0.08: return None  # flat: no clear subject
    return float(win.argmax() + k / 2)

def compute(data):
    """data: encoded image bytes. Returns dict(x, method, width, height, n) or None if undecodable/unavailable.
    method: portrait | faces | person | energy | center."""
    if not available(): return None
    cv2, np = _cv, __import__("numpy")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None: return None
    H, W = img.shape[:2]
    if W <= H * ASPECT * 1.02: return dict(x=0.5, method="portrait", width=W, height=H, n=0)
    s = min(1.0, 640 / max(W, H))
    small = cv2.resize(img, (max(1, int(W * s)), max(1, int(H * s))), interpolation=cv2.INTER_AREA) if s < 1 else img
    h, w = small.shape[:2]; cw = h * ASPECT
    for method, fn in (("faces", _faces), ("person", _people)):
        try: boxes = fn(small)
        except Exception: boxes = []
        cx, n = _pick(boxes, cw)
        if cx is not None:
            return dict(x=_span_focus(cx, w, cw), method=method, width=W, height=H, n=n, found=len(boxes),
                        conf=[round(b[5], 3) for b in boxes if len(b) > 5])
    try: cx = _energy(small, cw)
    except Exception: cx = None
    if cx is not None: return dict(x=_span_focus(cx, w, cw), method="energy", width=W, height=H, n=0)
    return dict(x=0.5, method="center", width=W, height=H, n=0)
