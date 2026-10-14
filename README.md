<p align="center"><img src="static/logo-full.png" alt="Film Tagger" width="420"></p>

# Film Tagger

**A private, local web app for cataloguing and richly tagging your own film collection.**
Point it at the folders where your video files live. It builds a searchable catalogue with posters and metadata, and lets you tag films in as much detail as you like, by hand or with help from AI. It runs entirely on your own computer. It is a catalogue, not a video player.

> **New to this? See [INSTALL.txt](INSTALL.txt)** for step-by-step Windows setup: download `FilmTaggerSetup.exe`, double-click, done. No Python install or admin rights needed.

## Features
- **Library scan:** recursively finds video files (mkv, mp4, avi, mov, wmv, m4v, mpg, ts, webm, vob, …) in any folders or drives and guesses the title and year from file or folder names. **Read-only:** your files are never moved, renamed or deleted. Rescans add new files and flag missing ones.
- **Rich film records:** title, year, director, cast, runtime, plot, poster, MPAA rating, your 1–10 rating, watched flag, notes, storage location and file paths. Everything is editable.
- **Unlimited tags** with user-editable categories (Genre, Content, Themes, Mood, Setting, Era…), autocomplete and bulk add/remove.
- **Powerful search:** tag logic (ALL / ANY / NOT), text search in any field, filters for year, rating, runtime, watched, MPAA and library folder. Searches can be saved.
- **Poster grid and sortable table**, fast with 10,000+ films.
- **Clickable people:** click any director, actor, writer, producer, cinematographer or composer to see all their films, with a role breakdown and sorting.
- **Your own poster art:** upload, drag-and-drop or paste an image URL. TMDB refreshes never replace it.
- **Original and alternative titles:** search finds films by original, TMDB alternative or your own "also known as" titles, ignoring accents.
- **Optional TMDB metadata:** with a free key it fetches posters, cast, plot, runtime and genres for one film or your whole library, and fills in MPAA ratings.
- **Smart tagging (AI):** queue films and let an AI suggest tags. Every suggestion waits for your approval. You can use it two ways:
  - **No key needed:** copy the ready-made prompt into any AI chatbot (ChatGPT, Grok, Claude, Gemini, …) and paste back its JSON reply.
  - **Bring your own key:** connect any OpenAI-compatible API (OpenAI, OpenRouter, xAI, or local Ollama / LM Studio) for one-click "Tag now".
- **Backup / restore** as JSON or CSV.

## Screenshots
_Screenshots coming soon._ <!-- Add images to a /docs folder and link them here. -->

## Install
- **Windows (recommended):** download and run **`FilmTaggerSetup.exe`**. It installs per user into `%LOCALAPPDATA%\Film Tagger` with a bundled Python 3.12, so no admin rights are needed. It adds Start Menu and (optionally) Desktop and sign-in shortcuts and an entry in *Apps* for uninstalling. Your library (`films.db` + `posters/`) lives in `%LOCALAPPDATA%\Film Tagger\data` and survives upgrades. See [INSTALL.txt](INSTALL.txt).
  - Silent install: `FilmTaggerSetup.exe /S [/D=C:\path] [/NODESKTOP] [/STARTUP] [/PORT=5051]`. Silent uninstall (keeps the library): `uninstall.exe /S`.
  - Build the installer yourself on Linux: `installer/build_installer.sh` (needs `makensis`, `pip`, `curl`, `unzip`) → `dist/FilmTaggerSetup.exe`.
- **Windows (manual):** install Python 3, unzip the source, double-click `Install Film Tagger.bat`.
- **Mac / Linux:** `python3 -m pip install -r requirements.txt`, then `python3 app.py`, then open http://localhost:5050.

## Smart tagging from the command line
```
python smarttag.py export-queue queue.json     # films you queued, as JSON
python smarttag.py import-tags tags.json       # AI reply -> pending tags for review
```
Reply format: `[{"film_id": 12, "tags": [{"name": "heist", "category": "Themes"}]}]`. See [SMART_TAG_PROMPT.txt](SMART_TAG_PROMPT.txt) for the full prompt.
REST equivalents: `GET /api/queue/export`, `POST /api/queue/import`, `GET /api/pending`, `POST /api/pending/review`.

## Privacy
- Everything stays on your computer in one SQLite file (`films.db`, plus a `posters/` folder for custom art). There is no account, telemetry or cloud service.
- The app only listens on `localhost` and rejects requests from other websites.
- Network requests only happen when **you** use them:
  - TMDB lookups, if you add a TMDB key;
  - your chosen AI provider, if you set one up. Only the film's title, year, director, file path, a short plot and its existing tags are sent.
- API keys are stored only in your local database.

## Tech
Python 3 + Flask + SQLite, with a single-page HTML/JS front end. There is no build step, and Flask is the only dependency.

## License
[MIT](LICENSE) © 2026 Kyle R. Hudson & Grok Bot
