@echo off
REM Double-click this to open a PowerShell already in the project folder,
REM with the bot commands listed and ready to type.
REM
REM Pin it to your taskbar: right-click -> Pin to taskbar.

cd /d "%~dp0"
powershell -NoExit -ExecutionPolicy Bypass -Command ^
  "Set-Location '%~dp0'; .\trade.ps1 help; Write-Host 'You are in the project folder. Type a command above.' -ForegroundColor Green"
