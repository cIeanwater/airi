@echo off
setlocal
cd /d "%~dp0"
set "HUMANNING_PYTHON=%~dp0local-models\airllm-cuda-venv\Scripts\python.exe"
if not exist "%HUMANNING_PYTHON%" (
  echo Prepared local Python was not found.
  echo See docs\ai\local-voice-conversation.zh-CN.md for setup.
  pause
  exit /b 1
)
"%HUMANNING_PYTHON%" "%~dp0scripts\start-local-voice.py" %*
if errorlevel 1 (
  echo Humanning could not start. Read the error above and .tools\local-voice-logs in the parent folder.
  pause
  exit /b 1
)
echo Humanning is running. Closing this window keeps it running.
timeout /t 4 >nul 2>nul
exit /b 0
