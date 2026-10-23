"""Download sturdiness: a file vanishing mid-check never aborts the gallery; long gallery folder names are shortened.
Run: python -m pytest tests/test_gallery_dl_fix.py"""
import os, sys, tempfile, time
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import findgallery as fg, gallery

LONG = os.path.join("D:" + os.sep, "Porn Scenes", "2 Chicks Same Time", "2 Chicks Same Time - 2019-12-13 - Izzy Lush, Maya Kendrick, Jay Smooth - "
        "Izzy Lush and Maya Kendrick Skip the Pool Party to Fuck Jay And Then Some More Words Here.mp4")

def test_short_name_stable_and_safe():
    d = gallery.canonical_dir(LONG)
    assert d == gallery.canonical_dir(LONG) and d.endswith(" - GALLERY") and gallery.is_gallery_dir(os.path.basename(d))
    assert len(d) <= gallery.PATH_SAFE - gallery.NAME_RESERVE and "~" in d and os.path.basename(d).startswith("2 Chicks Same Time - 2019-12-13")
    short = os.path.join("S", "Studio - 2020-01-01 - A - T.mp4")
    assert gallery.canonical_dir(short) == os.path.join("S", "Studio - 2020-01-01 - A - T - GALLERY")

def test_existing_long_gallery_still_found(tmp_path):
    v = os.path.join(str(tmp_path), "S - 2020-01-01 - " + "x" * 170 + ".mp4"); open(v, "w").close()
    assert gallery.full_dir(v) != gallery.canonical_dir(v)
    os.makedirs(gallery.full_dir(v))
    assert gallery.find(v, {".mp4"}) == ("canonical", gallery.full_dir(v))
    os.rmdir(gallery.full_dir(v)); os.makedirs(gallery.canonical_dir(v))
    assert gallery.find(v, {".mp4"}) == ("canonical", gallery.canonical_dir(v))

def test_vanishing_part_file_is_skipped(tmp_path, monkeypatch):
    d = str(tmp_path)
    for i in range(12): open(os.path.join(d, f"image_{i}.jpg"), "wb").write(os.urandom(20000 + i))
    open(os.path.join(d, "image_29.jpg.part"), "wb").write(b"x")
    real = os.path.getsize
    def gs(p):
        if p.endswith(".part"): raise FileNotFoundError(2, "The system cannot find the file specified", p)
        return real(p)
    monkeypatch.setattr(fg.os.path, "getsize", gs)
    assert fg.placeholders(d) == []  # the renamed .part is just skipped

class Proc:
    def __init__(self, cmd, **k): self.n = 0; self.returncode = None; self.pid = 1; self.dest = cmd[cmd.index("-D") + 1]
    def poll(self):
        self.n += 1
        if self.n < 4:
            open(os.path.join(self.dest, f"image_{self.n}.jpg"), "wb").write(os.urandom(20000 + self.n)); return None
        self.returncode = 0; return 0
    def wait(self, timeout=None): return self.returncode
    def terminate(self): self.returncode = -1
    def kill(self): pass

def test_download_survives_folder_errors(tmp_path, monkeypatch):
    fg.DL.clear(); fg.DL["running"] = False
    monkeypatch.setattr(fg, "set_links", lambda url: ([], [])); monkeypatch.setattr(fg, "expected_count", lambda url: None)
    monkeypatch.setattr(fg.time, "sleep", lambda s: None)
    calls = {"n": 0}; real = fg.count_files
    def flaky(d):
        calls["n"] += 1
        if calls["n"] == 2: raise FileNotFoundError(2, "gone", os.path.join(d, "image_29.jpg.part"))
        return real(d)
    monkeypatch.setattr(fg, "count_files", flaky)
    dest = os.path.join(str(tmp_path), "S - GALLERY")
    fg.run_download("https://vipergirls.to/threads/5", dest, popen=Proc)
    assert not fg.DL["error"] and fg.DL["files"] == 3 and not fg.DL["running"]

if __name__ == "__main__":  # runs without pytest too: python tests/test_gallery_dl_fix.py
    import pathlib
    class MP:
        def __init__(self): self.undo = []
        def setattr(self, o, n, v): self.undo.append((o, n, getattr(o, n))); setattr(o, n, v)
        def restore(self):
            for o, n, v in reversed(self.undo): setattr(o, n, v)
    for name, fn in list(globals().items()):
        if not name.startswith("test_"): continue
        mp = MP(); args = {"tmp_path": pathlib.Path(tempfile.mkdtemp()), "monkeypatch": mp}
        try: fn(**{k: args[k] for k in fn.__code__.co_varnames[:fn.__code__.co_argcount]})
        finally: mp.restore()
    print("OK")
