' Film Tagger launcher: starts the server hidden (if not already running) and opens it.
' Usage: FilmTagger.vbs           -> start server + open app window
'        FilmTagger.vbs /server   -> only start the server (used by the Windows Startup shortcut)
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
d = fso.GetParentFolderName(WScript.ScriptFullName)
serverOnly = False
If WScript.Arguments.Count > 0 Then If LCase(WScript.Arguments(0)) = "/server" Then serverOnly = True

' Installed with FilmTaggerSetup.exe: bundled Python in .\python, library data in .\data (kept on upgrade).
' Manual install with "Install Film Tagger.bat": .venv, data (films.db) next to app.py.
port = "5050"
pyw = d & "\python\pythonw.exe"
If fso.FileExists(pyw) Then
  dataDir = d & "\data"
  If Not fso.FolderExists(dataDir) Then fso.CreateFolder(dataDir)
  sh.Environment("PROCESS")("FILMTAGGER_DB") = dataDir & "\films.db"
  If fso.FileExists(dataDir & "\port.txt") Then
    t = Trim(fso.OpenTextFile(dataDir & "\port.txt").ReadLine())
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
