@echo off
title Uninstall Film Tagger
cd /d "%~dp0"
echo This removes the Film Tagger shortcuts and stops the app.
echo Your film catalogue (films.db) and your video files are NOT deleted.
echo.
choice /C YN /M "Continue"
if errorlevel 2 exit /b 0
call "%~dp0Stop Film Tagger.bat"
powershell -NoProfile -Command ^
  "foreach($f in 'Desktop','Programs','Startup'){ $p=[Environment]::GetFolderPath($f)+'\Film Tagger.lnk'; if(Test-Path $p){Remove-Item $p; 'Removed '+$p} }"
echo.
echo Shortcuts removed. To remove Film Tagger completely, delete this folder:
echo   %~dp0
echo (Keep a copy of films.db first if you might want your catalogue back.)
pause
