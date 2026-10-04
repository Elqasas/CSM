' Starts the CSM_APP Django dev server with no visible console window.
' Used by the "CSM App Dev Server" Task Scheduler task (runs at logon).
'
' It also starts qcluster, the django-q worker that runs the scheduled jobs
' (00:30 health, 01:00 tasks, 07:00 digest, reminders every 5 minutes).
' Without it the schedules sit in the database and nothing fires - no
' morning digest, no reminders, no overnight rescoring.
Set objShell = CreateObject("WScript.Shell")
projectDir = "C:\Users\a.elqasas\Desktop\CSM_APP Version II"
python = """.venv\Scripts\python.exe"""
objShell.Run "cmd /c cd /d """ & projectDir & """ && " & python & " manage.py runserver", 0, False
objShell.Run "cmd /c cd /d """ & projectDir & """ && " & python & " manage.py qcluster", 0, False
