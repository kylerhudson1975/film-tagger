"""Height units setting: metric / imperial / both (default) on people pages. Conversion rounds the total inches
(so no 5'12"), reads '162cm', 162, '1.62 m', feet/inches strings, and leaves unreadable values as typed.
Temp DB, no network. Run: python tests/test_height_units.py"""
import io, os, sys, tempfile
os.environ["FILMTAGGER_DB"] = os.path.join(tempfile.mkdtemp(), "films.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app, people as P
F = P.fmt_height
# parsing
assert P.parse_height_cm("162cm") == 162 and P.parse_height_cm("162 cm") == 162 and P.parse_height_cm(162) == 162
assert P.parse_height_cm("162") == 162 and P.parse_height_cm(162.4) == 162.4 and P.parse_height_cm("1.62 m") == 162
assert abs(P.parse_height_cm("5'4\"") - 162.56) < 1e-9 and abs(P.parse_height_cm("5 ft 4 in") - 162.56) < 1e-9
assert abs(P.parse_height_cm("5′4″") - 162.56) < 1e-9 and abs(P.parse_height_cm("64 in") - 162.56) < 1e-9 and P.parse_height_cm("6'") == 6 * 12 * 2.54
for bad in (None, "", "tall", "about 5 foot", "5", 0, 999, True, "162 lbs"): assert P.parse_height_cm(bad) is None, bad
# formatting
assert F("162cm") == "5′4″ (162 cm)" and F("162cm", "metric") == "162 cm" and F("162cm", "imperial") == "5′4″"
assert F(162, "both") == "5′4″ (162 cm)" and F("5'4\"", "metric") == "163 cm"
assert F(182, "imperial") == "6′0″"            # 71.65 in rounds to 72 -> 6'0", not 5'12"
assert F(181, "imperial") == "5′11″" and F(152.4, "imperial") == "5′0″" and F(30.48 * 6, "imperial") == "6′0″"
assert F(167.6, "imperial") == "5′6″"           # 65.98 in -> 66
assert F("tall") == "tall" and F("tall", "metric") == "tall" and F(None) is None and F("") == ""
# setting: default both, validated, used on the person page
cl = app.app.test_client()
assert cl.get("/api/settings").json["height_units"] == "both"
pid = cl.post("/api/people/new", data={"name": "Synthetic Person", "height": "162cm", "photo": (io.BytesIO(b"\x89PNG x"), "x.png")},
              content_type="multipart/form-data").json["id"]
pp = lambda: cl.get(f"/api/person/{pid}").json["person"]
assert pp()["height"] == "162cm" and pp()["height_display"] == "5′4″ (162 cm)"
for u, want in (("metric", "162 cm"), ("imperial", "5′4″"), ("both", "5′4″ (162 cm)")):
    assert cl.put("/api/settings", json={"height_units": u}).json["height_units"] == u
    assert pp()["height_display"] == want and pp()["height"] == "162cm"  # stored value never changed
assert cl.put("/api/settings", json={"height_units": "furlongs"}).status_code == 400
assert cl.get("/api/settings").json["height_units"] == "both"
print("OK test_height_units")
