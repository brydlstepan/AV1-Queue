' Runs the given PowerShell script completely invisibly (no console flash).
' Usage: wscript.exe launch_hidden.vbs "<path to .ps1>"
scriptPath = WScript.Arguments(0)
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File " & Chr(34) & scriptPath & Chr(34)
CreateObject("WScript.Shell").Run cmd, 0, False
