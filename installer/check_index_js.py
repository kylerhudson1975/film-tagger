"""Release check: every inline <script> in static/index.html must parse (node --check).
A single syntax error blanks the whole app, so the installer build stops here first."""
import os, re, subprocess, sys, tempfile
src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "index.html")
html = open(src, encoding="utf-8").read()
blocks = [m.group(2) for m in re.finditer(r"<script(\s[^>]*)?>(.*?)</script>", html, re.S | re.I)
          if not re.search(r"\bsrc\s*=", m.group(1) or "") and "module" not in (m.group(1) or "")]
bad = 0
for i, js in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f: f.write(js)
    r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True); os.unlink(f.name)
    if r.returncode: bad += 1; print(f"index.html inline script #{i + 1}: syntax error\n{r.stderr}", file=sys.stderr)
print(f"index.html: {len(blocks)} inline script(s) checked, {bad} with errors")
sys.exit(1 if bad or not blocks else 0)
