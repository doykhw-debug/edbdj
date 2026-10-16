@echo off
rem Deploy this program to the Oracle server (ssh haetsal): backup PC data, upload, install, start radio.service.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\pc_deploy.ps1" %*
if errorlevel 1 pause
