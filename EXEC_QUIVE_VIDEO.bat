@echo off
chcp 65001 >nul
title EXEC_QUIVE_VIDEO
cd /d "%~dp0"
where pythonw >nul 2>&1
if errorlevel 1 (
  start "EXEC_QUIVE_VIDEO" python "%~dp0EXEC_QUIVE_VIDEO.py"
) else (
  start "EXEC_QUIVE_VIDEO" pythonw "%~dp0EXEC_QUIVE_VIDEO.py"
)
