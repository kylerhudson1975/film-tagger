@echo off
cd /d "%~dp0"
set "FT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=$env:FT_DIR.TrimEnd('\'); $n=0; Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'pythonw.exe','python.exe' } | Where-Object { $_.CommandLine -and $_.CommandLine -like '*app.py*' -and $_.CommandLine -like ('*'+$d+'*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $n++ }; if($n){'Film Tagger stopped.'}else{'Film Tagger was not running.'}"
timeout /t 3 >nul
