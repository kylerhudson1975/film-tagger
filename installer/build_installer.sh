#!/bin/sh
# Builds dist/FilmTaggerSetup.exe on Linux (needs: makensis, pip, curl, unzip).
set -e
HERE=$(cd "$(dirname "$0")" && pwd); SRC=$(dirname "$HERE"); OUT=${OUT:-$SRC/dist}; B=$(mktemp -d)
PYV=3.12.10
curl -sSfL -o "$B/py.zip" "https://www.python.org/ftp/python/$PYV/python-$PYV-embed-amd64.zip"
mkdir -p "$B/stage/python" && unzip -q "$B/py.zip" -d "$B/stage/python"
printf 'python312.zip\r\n.\r\nLib\\site-packages\r\n..\r\n\r\nimport site\r\n' > "$B/stage/python/python312._pth"
pip install -q --target "$B/stage/python/Lib/site-packages" --platform win_amd64 --python-version 3.12 \
  --implementation cp --only-binary=:all: -r "$SRC/requirements.txt" colorama
rm -rf "$B/stage/python/Lib/site-packages/bin"
for f in app.py db.py smarttag.py FilmTagger.vbs LICENSE README.md INSTALL.txt SMART_TAG_PROMPT.txt requirements.txt; do cp "$SRC/$f" "$B/stage/"; done
mkdir "$B/stage/static" && cp "$SRC"/static/* "$B/stage/static/"
find "$B/stage" -name __pycache__ -prune -exec rm -rf {} +
mkdir -p "$OUT"
makensis -V2 -DSTAGE="$B/stage" -DOUTFILE="$OUT/FilmTaggerSetup.exe" "$HERE/FilmTagger.nsi"
rm -rf "$B"; ls -l "$OUT/FilmTaggerSetup.exe"
