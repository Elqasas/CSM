' Starts the CSM_APP Django dev server with no visible console window.
' Used by the "CSM App Dev Server" Task Scheduler task (runs at logon).
Set objShell = CreateObject("WScript.Shell")
projectDir = "C:\Users\a.elqasas\Desktop\CSM_APP Version II"
cmd = "cmd /c cd /d """ & projectDir & """ && "".venv\Scripts\python.exe"" manage.py runserver"
objShell.Run cmd, 0, False
