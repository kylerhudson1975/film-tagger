"""AI upscale (upscale.py) with the Real-ESRGAN exe mocked: discrete GPU first, fallback to the other GPU, size cap,
missing exe message, and Gallery Save editing the upscaled pixels while the original is kept in _originals.
Run: python tests/test_upscale.py"""
import os, sys, tempfile, time, unittest
from unittest import mock
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
if "FILMTAGGER_DB" not in os.environ: os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, ROOT)
from PIL import Image
import upscale as U, galleryadjust as G

LIST = "[0 AMD Radeon(TM) Graphics]  queueC=1\n[1 NVIDIA GeForce RTX 2050]  queueC=2\n"

def fake_run(fail_gpus=()):
    calls = []
    def run(exe, src, out, gpu, job):
        calls.append(gpu)
        if gpu in fail_gpus: return 1, LIST + "vkQueueSubmit failed\n"
        with Image.open(src) as im: im.resize((im.size[0] * 4, im.size[1] * 4)).save(out)
        return 0, LIST + "50.00%\n"
    return run, calls

class Upscale(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(); exe = os.path.join(self.d, "realesrgan-ncnn-vulkan.exe"); open(exe, "wb").close()
        os.environ["FILMTAGGER_REALESRGAN"] = exe; U._GPU.clear()
        self.img = os.path.join(self.d, "a.jpg"); Image.new("RGB", (100, 150), (200, 50, 50)).save(self.img)
    def wait(self, t):
        for _ in range(200):
            if U.JOBS[t]["state"] != "running": return U.JOBS[t]
            time.sleep(0.02)
        self.fail("timeout")

    def test_gallery_save_uses_upscaled_pixels_and_keeps_original(self):
        run, calls = fake_run()
        with mock.patch.object(U, "_run", run):
            r, st = G.handle(self.d, "a.jpg", "upscale", {"scale": 2}); self.assertEqual(st, 200)
            j = self.wait(r["token"])
        self.assertEqual((j["state"], j["width"], j["height"]), ("done", 200, 300))
        self.assertEqual(calls[-1], 1)  # NVIDIA (discrete) chosen after the probe
        self.assertEqual(Image.open(self.img).size, (100, 150))  # nothing changed before Save
        r, st = G.handle(self.d, "a.jpg", "save", {"upscale": r["token"], "rot": 1})
        self.assertEqual(st, 200, r); self.assertEqual(Image.open(self.img).size, (300, 200))  # upscaled, then rotated
        self.assertEqual(Image.open(G.original_path(self.img)).size, (100, 150))
        self.assertEqual(G.handle(self.d, "a.jpg", "revert", {})[1], 200); self.assertEqual(Image.open(self.img).size, (100, 150))

    def test_token_only_for_its_own_photo(self):
        other = os.path.join(self.d, "b.jpg"); Image.new("RGB", (10, 10)).save(other)
        with mock.patch.object(U, "_run", fake_run()[0]):
            t = G.handle(self.d, "a.jpg", "upscale", {"scale": 4})[0]["token"]; self.wait(t)
        r, st = G.handle(self.d, "b.jpg", "save", {"upscale": t}); self.assertEqual(st, 400); self.assertIn("Upscale again", r["error"])

    def test_falls_back_to_other_gpu(self):
        run, calls = fake_run(fail_gpus=(1,))
        with mock.patch.object(U, "_run", run):
            j = self.wait(U.start(self.img, 4))
        self.assertEqual(j["state"], "done"); self.assertEqual(calls[-2:], [1, 0]); self.assertIn("first GPU failed", j["note"])

    def test_all_gpus_fail_is_a_clear_error(self):
        with mock.patch.object(U, "_run", fake_run(fail_gpus=(0, 1))[0]):
            j = self.wait(U.start(self.img, 4))
        self.assertEqual(j["state"], "error"); self.assertIn("no CPU mode", j["error"])

    def test_cap_and_missing_exe(self):
        with mock.patch.object(U, "MAX_SIDE", 300), mock.patch.object(U, "_run", fake_run()[0]):
            j = self.wait(U.start(self.img, 4))
        self.assertEqual((j["width"], j["height"]), (200, 300))  # 4x capped to a 300px long side
        os.environ["FILMTAGGER_REALESRGAN"] = os.path.join(self.d, "nope.exe")
        j = self.wait(U.start(self.img, 2)); self.assertIn("not installed", j["error"])
        with self.assertRaises(ValueError): U.start(self.img, 3)

if __name__ == "__main__": unittest.main()
