' ============================================================
'  Profile AI Assistant - single entry point (NO console window)
'  Usage : wscript.exe appctl.vbs <command> [options]
'          double-click this file  =  "start"
'  Command: start | stop | restart | status | open |
'           backup | shortcut
'  Logs   : logs\start.log , logs\stop.log , logs\<task>.log
'  Notes  : a message box appears ONLY when something fails.
'           Want to watch the progress?  Use appctl.bat instead.
' ============================================================
Option Explicit

Dim fso, sh, root, py, target, cmd, extra, i
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
py = root & "\.venv\Scripts\python.exe"
target = root & "\scripts\appctl.py"

' first argument = command; anything starting with "-" is treated as an option
cmd = "start"
If WScript.Arguments.Count > 0 Then
    If Left(Trim(WScript.Arguments(0)), 1) <> "-" Then
        cmd = LCase(Trim(WScript.Arguments(0)))
    End If
End If

extra = ""
For i = 1 To WScript.Arguments.Count - 1
    extra = extra & " " & WScript.Arguments(i)
Next

If Not fso.FileExists(py) Then
    MsgBox "Python environment not found:" & vbCrLf & py & vbCrLf & vbCrLf & "Run setup.cmd first.", 16, "Profile AI Assistant"
    WScript.Quit 3
End If

If Not fso.FileExists(target) Then
    MsgBox "Launcher not found:" & vbCrLf & target, 16, "Profile AI Assistant"
    WScript.Quit 3
End If

sh.CurrentDirectory = root
' --silent  : write logs, show a message box only when something fails
' --no-pause: never wait for a key press (there is no console window at all)
sh.Run """" & py & """ """ & target & """ " & cmd & " --silent --no-pause" & extra, 0, False
