# Third-party software

## gallery-dl
- Project: https://github.com/mikf/gallery-dl
- Copyright: Mike Fährmann and contributors
- License: GNU General Public License v2.0 (https://github.com/mikf/gallery-dl/blob/master/LICENSE)
- Use in Film Tagger: installed from PyPI next to the app (listed in `requirements.txt`, included in the Windows installer's bundled Python) and run only as a separate program (`python -m gallery_dl`) by the Find Gallery download. Film Tagger never imports or links gallery-dl's code. Its source is available at the project link above.

Other Python packages installed from `requirements.txt` (Flask, OpenCV, NumPy and their dependencies) keep their own licenses, found in each package's `*.dist-info` folder.


## Real-ESRGAN (realesrgan-ncnn-vulkan)
- Source: https://github.com/xinntao/Real-ESRGAN (release v0.2.5.0, `realesrgan-ncnn-vulkan-20220424-windows.zip`, SHA256 `abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d`)
- License: BSD-3-Clause, © 2021 Xintao Wang
- Not bundled: downloaded on first use of Upscale (Windows) into the Film Tagger data folder and run as a separate program.
