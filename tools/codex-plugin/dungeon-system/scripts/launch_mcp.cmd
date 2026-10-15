@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "DUNGEON_SERVER=%~dp0..\mcp\server.py"
if defined DUNGEON_PYTHON goto :configured_probe
if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" (
  "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
  if not errorlevel 1 goto :codex
)
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
if not errorlevel 1 goto :launcher
python -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
if not errorlevel 1 goto :path
>&2 echo Dungeon System MCP requires Python 3.11 or newer.
exit /b 9009

:configured_probe
if not exist "%DUNGEON_PYTHON%" goto :configured_error
"%DUNGEON_PYTHON%" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >nul 2>nul
if errorlevel 1 goto :configured_error
goto :configured

:configured_error
>&2 echo DUNGEON_PYTHON must name a working Python 3.11 or newer executable.
exit /b 9009

:configured
"%DUNGEON_PYTHON%" "%DUNGEON_SERVER%"
exit /b %errorlevel%

:codex
"%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" "%DUNGEON_SERVER%"
exit /b %errorlevel%

:launcher
py -3 "%DUNGEON_SERVER%"
exit /b %errorlevel%

:path
python "%DUNGEON_SERVER%"
exit /b %errorlevel%
