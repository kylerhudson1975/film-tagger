"""Make stills keep the video's display aspect ratio (no forced square; anamorphic uses SAR).
Uses ffmpeg lavfi test patterns only (no real/adult media). Run: python tests/test_stills_aspect.py"""
import os, sys, tempfile, subprocess, struct
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stills

def jpg_size(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:2] == b"\xff\xd8", path
    i = 2
    while i < len(data) - 8:
        if data[i] != 0xff: i += 1; continue
        m = data[i + 1]
        if m in (0xc0, 0xc1, 0xc2):  # SOF
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if m in (0xd8, 0xd9): i += 2; continue
        if i + 3 >= len(data): break
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        i += 2 + seglen
    raise AssertionError("no SOF in " + path)

def near(a, b, tol=0.01):
    return abs(a - b) / b <= tol

ff = stills.find_ffmpeg()[0] or "ffmpeg"
assert subprocess.run([ff, "-version"], capture_output=True).returncode == 0

td = tempfile.mkdtemp()
cases = [
    # label, ffmpeg args to build a short clip, expected display ratio w/h
    ("16x9", ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24:duration=2", "-pix_fmt", "yuv420p"], 320 / 180),
    ("4x3", ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2", "-pix_fmt", "yuv420p"], 320 / 240),
    # anamorphic: stored 240x240 with SAR 16:9 -> display 426.6x240 ≈ 16:9
    ("anamorphic_square", ["-f", "lavfi", "-i", "testsrc2=size=240x240:rate=24:duration=2",
                           "-pix_fmt", "yuv420p", "-vf", "setsar=16/9"], (240 * 16 / 9) / 240),
]
for label, args, want in cases:
    vid = os.path.join(td, label + ".mp4")
    out = os.path.join(td, label + ".jpg")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args, vid], check=True)
    d, w, h, fps, sar = stills.probe(ff, vid)
    assert d and w and h, (label, d, w, h)
    assert stills.save_frame(ff, vid, 0.0, 0, out), label
    jw, jh = jpg_size(out)
    got = jw / jh
    assert near(got, want), (label, jw, jh, got, want, sar)
    # never a forced 1:1 unless the source display really is square
    if abs(want - 1.0) > 0.02:
        assert abs(got - 1.0) > 0.02, (label, jw, jh)
    print(f"ok {label}: {jw}x{jh} ratio={got:.4f} (want {want:.4f})")

# display_size helper
assert stills.display_size(2160, 2160, 16 / 9) == (3840, 2160)
assert stills.display_size(1920, 1080, 1.0) == (1920, 1080)
print("ok")
