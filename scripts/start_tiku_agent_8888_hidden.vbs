Dim shell
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""F:\cc\7-题库检索\.worktrees\demo-shadow-8888\scripts\tiku_agent_watchdog_8888.ps1"" -PythonExe ""C:\Users\31492\AppData\Local\Programs\Python\Python312\python.exe""", 0, False
