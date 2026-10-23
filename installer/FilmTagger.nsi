; Film Tagger - Windows installer (NSIS 3). Build with installer/build_installer.sh
; Per-user install, no admin rights. Silent options: /S /D=<dir> /NODESKTOP /STARTUP /PORT=<n>
Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"

!define APPNAME "Film Tagger"
!define APPVER "1.2.0"
!define PUBLISHER "Kyle R. Hudson & Grok Bot"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\FilmTagger"
!define REGKEY "Software\FilmTagger"
!ifndef STAGE
  !define STAGE "stage"
!endif
!ifndef OUTFILE
  !define OUTFILE "FilmTaggerSetup.exe"
!endif

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\${APPNAME}"
InstallDirRegKey HKCU "${REGKEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "${APPNAME} ${APPVER}"
VIProductVersion "${APPVER}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "(c) 2026 ${PUBLISHER} - MIT License"
VIAddVersionKey "FileDescription" "${APPNAME} Setup"
VIAddVersionKey "FileVersion" "${APPVER}"
VIAddVersionKey "ProductVersion" "${APPVER}"

!define MUI_ICON "${STAGE}\static\favicon.ico"
!define MUI_UNICON "${STAGE}\static\favicon.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This will install ${APPNAME} ${APPVER}, a free tool for cataloguing and tagging your film collection.$\r$\n$\r$\nEverything runs on this computer. No administrator rights and no separate Python installation are needed.$\r$\n$\r$\nBy Kyle R. Hudson && Grok Bot. Free, open-source software (MIT License).$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Launch ${APPNAME}"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\INSTALL.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Show the getting-started guide"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${STAGE}\LICENSE"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Var Port

; Stop a Film Tagger server that runs from this install folder (needed before upgrading/uninstalling).
!macro StopServer
  nsExec::Exec `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $$_.CommandLine -and $$_.CommandLine -like '*app.py*' -and $$_.CommandLine -like '*$INSTDIR\*' } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force }"`
  Pop $0
  Sleep 800
!macroend

Function LaunchApp
  Exec '"$SYSDIR\wscript.exe" "$INSTDIR\FilmTagger.vbs"'
FunctionEnd

Section "${APPNAME} (required)" SecMain
  SectionIn RO
  !insertmacro StopServer
  ; replace program files, never the user's data folder
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\static"
  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*.*"
  CreateDirectory "$INSTDIR\data"
  ${If} $Port != ""
    FileOpen $1 "$INSTDIR\data\port.txt" w
    FileWrite $1 "$Port"
    FileClose $1
  ${EndIf}
  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$SYSDIR\wscript.exe" '"$INSTDIR\FilmTagger.vbs"' "$INSTDIR\static\favicon.ico" 0
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall ${APPNAME}.lnk" "$INSTDIR\uninstall.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${REGKEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${APPVER}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\static\favicon.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" "$0"
SectionEnd

Section "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$SYSDIR\wscript.exe" '"$INSTDIR\FilmTagger.vbs"' "$INSTDIR\static\favicon.ico" 0
  WriteRegDWORD HKCU "${REGKEY}" "DesktopShortcut" 1
SectionEnd

Section /o "Start Film Tagger when I sign in to Windows" SecStartup
  CreateShortcut "$SMSTARTUP\${APPNAME}.lnk" "$SYSDIR\wscript.exe" '"$INSTDIR\FilmTagger.vbs" /server' "$INSTDIR\static\favicon.ico" 0
  WriteRegDWORD HKCU "${REGKEY}" "StartupShortcut" 1
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} "The app, a private copy of Python and the Start Menu shortcut."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "Put a Film Tagger icon on the Desktop."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecStartup} "Start the Film Tagger server quietly in the background when you sign in, so it opens instantly."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Function .onInit
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/NODESKTOP" $R1
  ${IfNot} ${Errors}
    !insertmacro UnselectSection ${SecDesktop}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/STARTUP" $R1
  ${IfNot} ${Errors}
    !insertmacro SelectSection ${SecStartup}
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/PORT=" $Port
FunctionEnd

Section "Uninstall"
  !insertmacro StopServer
  ReadRegDWORD $0 HKCU "${REGKEY}" "DesktopShortcut"
  ${If} $0 == 1
    Delete "$DESKTOP\${APPNAME}.lnk"
  ${EndIf}
  ReadRegDWORD $0 HKCU "${REGKEY}" "StartupShortcut"
  ${If} $0 == 1
    Delete "$SMSTARTUP\${APPNAME}.lnk"
  ${EndIf}
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\static"
  RMDir /r "$INSTDIR\__pycache__"
  Delete "$INSTDIR\*.py"
  Delete "$INSTDIR\*.txt"
  Delete "$INSTDIR\*.md"
  Delete "$INSTDIR\*.vbs"
  Delete "$INSTDIR\LICENSE"
  ${If} ${FileExists} "$INSTDIR\data\*.*"
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your film library (database and custom posters)?$\r$\n$\r$\nChoose No to keep it in:$\r$\n$INSTDIR\data$\r$\n(it will be used again if you reinstall). Your video files are never touched." /SD IDNO IDYES deldata IDNO keepdata
    deldata:
      RMDir /r "$INSTDIR\data"
    keepdata:
  ${EndIf}
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "${REGKEY}"
SectionEnd
