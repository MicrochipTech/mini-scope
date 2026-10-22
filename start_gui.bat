@echo off
rem start the mini-scope GUI; the first start sets up the Python environment
rem   start_gui.bat --sim   no hardware: PC model of scope.c (needs tests\sim.dll)
setlocal
call "%~dp0tools\setup_env.bat" || (pause & exit /b 1)
if "%MINISCOPE_VENV%"=="" set MINISCOPE_VENV=%~dp0.venv
"%MINISCOPE_VENV%\Scripts\python.exe" "%~dp0gui\scope_gui.py" %*
