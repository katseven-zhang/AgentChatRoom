@echo off
for %%I in ("%~dp0..") do set "AGENTCHATROOM_ROOT=%%~fI"
set "AGENTCHATROOM_PYTHON=%AGENTCHATROOM_ROOT%\.venv\Scripts\python.exe"
if not defined AGENTCHATROOM_DATA_DIR set "AGENTCHATROOM_DATA_DIR=%AGENTCHATROOM_ROOT%\.agentchatroom\runtime"

if not exist "%AGENTCHATROOM_PYTHON%" (
  echo [AgentChatRoom] Creating local Python environment...
  where py >nul 2>&1
  if errorlevel 1 (
    python -m venv "%AGENTCHATROOM_ROOT%\.venv"
  ) else (
    py -3 -m venv "%AGENTCHATROOM_ROOT%\.venv"
  )
  if errorlevel 1 exit /b 1
)

"%AGENTCHATROOM_PYTHON%" -c "import agentchatroom" >nul 2>&1
if errorlevel 1 (
  echo [AgentChatRoom] Installing application dependencies...
  "%AGENTCHATROOM_PYTHON%" -m pip install -e "%AGENTCHATROOM_ROOT%"
  if errorlevel 1 exit /b 1
)

exit /b 0
