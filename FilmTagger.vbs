' Film Tagger launcher: starts the server hidden (if not already running) and opens it.
' Usage: FilmTagger.vbs           -> start server + open app window
'        FilmTagger.vbs /server   -> only start the server (used by the Windows Startup shortcut)
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
serverOnly = False
If WScript.Arguments.Count > 0 Then If LCase(WScript.Arguments(0)) = "/server" Then serverOnly = True

' Installed with FilmTaggerSetup.exe: bundled Python in .\python, library data in
' %LOCALAPPDATA%\Film Tagger Data (outside the install folder so updates / reinstalls / install
' moves never wipe custom posters or person photos). Older installs kept data in .\data; that
' folder is copied once into the new location (never deleted).
' Manual install with "Install Film Tagger.bat": .venv, data (films.db) next to app.py.
port = "5050"
pyw = d & "\python\pythonw.exe"
If fso.FileExists(pyw) Then
  dataDir = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Film Tagger Data"
  oldData = d & "\data"
  If Not fso.FolderExists(dataDir) Then fso.CreateFolder(dataDir)
  ' One-time copy of the old install\data library (robocopy skips existing files; films.db last via /XF then copy).
  If Not fso.FileExists(dataDir & "\films.db") And fso.FileExists(oldData & "\films.db") Then
    sh.Run "cmd /c robocopy """ & oldData & """ """ & dataDir & """ /E /COPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS /XF films.db & copy /Y """ & oldData & "\films.db"" """ & dataDir & "\films.db""", 0, True
  End If
  sh.Environment("PROCESS")("FILMTAGGER_DB") = dataDir & "\films.db"
  sh.Environment("PROCESS")("FILMTAGGER_DATA") = dataDir
  portFile = ""
  If fso.FileExists(dataDir & "\port.txt") Then
    portFile = dataDir & "\port.txt"
  ElseIf fso.FileExists(oldData & "\port.txt") Then
    portFile = oldData & "\port.txt"
  End If
  If portFile <> "" Then
    t = Trim(fso.OpenTextFile(portFile).ReadLine())
    If IsNumeric(t) And t <> "" Then port = CStr(CLng(t))
  End If
Else
  pyw = d & "\.venv\Scripts\pythonw.exe"
  If Not fso.FileExists(pyw) Then
    MsgBox "Film Tagger is not installed yet." & vbCrLf & vbCrLf & "Please run FilmTaggerSetup.exe (or 'Install Film Tagger.bat' in this folder):" & vbCrLf & d, 48, "Film Tagger"
    WScript.Quit 1
  End If
End If
sh.Environment("PROCESS")("PORT") = port

' Already running? Ask the port (a process check can be fooled by another Film Tagger copy).
Function Up()
  Up = False
  On Error Resume Next
  Set x = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  x.setTimeouts 1000, 1000, 2000, 2000
  x.Open "GET", "http://127.0.0.1:" & port & "/", False
  x.Send
  If Err.Number = 0 Then If x.Status = 200 Then Up = True
  On Error GoTo 0
End Function
If Not Up() Then
  sh.CurrentDirectory = d
  sh.Run """" & pyw & """ """ & d & "\app.py""", 0, False
  If Not serverOnly Then
    For i = 1 To 30
      WScript.Sleep 500
      If Up() Then Exit For
    Next
  End If
End If
If serverOnly Then WScript.Quit 0

url = "http://localhost:" & port
chrome = ""
For Each c In Array(sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\Google\Chrome\Application\chrome.exe", sh.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Google\Chrome\Application\chrome.exe", sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Google\Chrome\Application\chrome.exe")
  If chrome = "" And fso.FileExists(c) Then chrome = c
Next
If chrome <> "" Then
  sh.Run """" & chrome & """ --start-maximized " & url, 3, False
Else
  sh.Run url, 3, False
End If
