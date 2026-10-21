# Third-party software

## gallery-dl
- Project: https://github.com/mikf/gallery-dl
- Copyright: Mike Fährmann and contributors
- License: GNU General Public License v2.0 (https://github.com/mikf/gallery-dl/blob/master/LICENSE)
- Use in Film Tagger: installed from PyPI next to the app (listed in `requirements.txt`, included in the Windows installer's bundled Python) and run only as a separate program (`python -m gallery_dl`) by the Find Gallery download. Film Tagger never imports or links gallery-dl's code. Its source is available at the project link above.

Other Python packages installed from `requirements.txt` (Flask, OpenCV, NumPy and their dependencies) keep their own licenses, found in each package's `*.dist-info` folder.
