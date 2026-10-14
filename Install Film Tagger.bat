@echo off
title Install Film Tagger
cd /d "%~dp0"
echo.
echo  ==========================================
echo    Film Tagger - installer
echo  ==========================================
echo.
echo  Installing into: %~dp0
echo.

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY ( py -3 --version >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  echo  [!] Python was not found.
  echo.
  echo      1. Go to https://www.python.org/downloads/ and install Python 3.
  echo      2. On the FIRST installer screen tick "Add python.exe to PATH".
  echo      3. Then double-click this installer again.
  echo.
  pause
  exit /b 1
)
echo  Found: & %PY% --version
echo.
echo  Step 1 of 3: creating a private Python environment...
if not exist ".venv\Scripts\python.exe" ( %PY% -m venv .venv || goto :fail )
echo  Step 2 of 3: installing Flask (needs internet, about a minute)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

echo  Step 3 of 3: creating the Desktop shortcut...
set "FT_DIR=%~dp0"
set "FT_STARTUP=0"
choice /C YN /N /M "  Start Film Tagger automatically when Windows starts? [Y/N] "
if errorlevel 2 ( set "FT_STARTUP=0" ) else ( set "FT_STARTUP=1" )
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=$env:FT_DIR.TrimEnd('\'); $sh=New-Object -ComObject WScript.Shell;" ^
  "function mk($path,$a){ $q=[char]34; $l=$sh.CreateShortcut($path); $l.TargetPath=$env:WINDIR+'\System32\wscript.exe'; $l.Arguments=($q+$d+'\FilmTagger.vbs'+$q+' '+$a).Trim(); $l.WorkingDirectory=$d; $l.IconLocation=$d+'\static\favicon.ico'; $l.Description='Film Tagger'; $l.Save() }" ^
  "mk ([Environment]::GetFolderPath('Desktop')+'\Film Tagger.lnk') '';" ^
  "mk ([Environment]::GetFolderPath('Programs')+'\Film Tagger.lnk') '';" ^
  "if($env:FT_STARTUP -eq '1'){ mk ([Environment]::GetFolderPath('Startup')+'\Film Tagger.lnk') '/server' }" || goto :fail

echo.
echo  Done!  A "Film Tagger" icon is now on your Desktop and in the Start menu.
echo  Starting Film Tagger now...
start "" wscript.exe "%~dp0FilmTagger.vbs"
echo.
pause
exit /b 0

:fail
echo.
echo  [!] Something went wrong (see the messages above). See INSTALL.txt, "Troubleshooting".
pause
exit /b 1
