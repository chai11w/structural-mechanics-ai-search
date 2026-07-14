Option Explicit

Dim shell, fso, projectDir, pythonExe, port, runtimeDir, command, exitCode

If WScript.Arguments.Count <> 2 Then
  WScript.Quit 2
End If

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

projectDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonExe = shell.ExpandEnvironmentStrings("%LocalAppData%") & "\Programs\Python\Python312\python.exe"
port = WScript.Arguments(0)
runtimeDir = WScript.Arguments(1)

shell.CurrentDirectory = projectDir
command = Quote(pythonExe) & " -B scripts\run_tiku_agent_demo.py --host 127.0.0.1 --port " & port _
  & " --intent-version v2 --runtime-dir " & Quote(runtimeDir)

exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode

Function Quote(value)
  Quote = Chr(34) & value & Chr(34)
End Function
