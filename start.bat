@echo off
rem Starts Film Tagger (installs it first if needed). Runs hidden; use "Stop Film Tagger.bat" or Settings > Quit to stop.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" ( call "Install Film Tagger.bat" & exit /b )
start "" wscript.exe "%~dp0FilmTagger.vbs"
