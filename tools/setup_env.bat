@echo off
rem setup_env.bat - creates the Python environment for mini-scope on first use
rem   .venv\ next to this project, packages from gui\requirements.txt
rem   called by start_gui.bat and start_sim.bat; reinstalls when requirements.txt changes
rem   set MINISCOPE_VENV=<folder> to put the environment elsewhere
setlocal
set ROOT=%~dp0..
if "%MINISCOPE_VENV%"=="" set MINISCOPE_VENV=%ROOT%\.venv
set VPY=%MINISCOPE_VENV%\Scripts\python.exe
set REQ=%ROOT%\gui\requirements.txt
set STAMP=%MINISCOPE_VENV%\requirements.installed

if exist "%VPY%" if exist "%STAMP%" (
    fc /b "%REQ%" "%STAMP%" >nul 2>&1 && exit /b 0
)

if not exist "%VPY%" (
    rem find Python 3.9 or newer: the py launcher first, then python on PATH
    set PY=
    py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1 && set PY=py -3
    if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1 && set PY=python
    if not defined PY (
        echo Python 3.9 or newer was not found. Install it from https://www.python.org
        echo and tick "Add python.exe to PATH", then run this again.
        exit /b 1
    )
    echo Creating Python environment in "%MINISCOPE_VENV%" ...
    call %%PY%% -m venv "%MINISCOPE_VENV%" || (echo Could not create the environment. & exit /b 1)
)

echo Installing Python packages (first start only, takes a minute) ...
"%VPY%" -m pip install --disable-pip-version-check -q -r "%REQ%" || (
    echo Package installation failed, see the messages above.
    echo Behind a proxy: set HTTPS_PROXY=http://proxy:port and run this again.
    exit /b 1
)
copy /y "%REQ%" "%STAMP%" >nul
echo Python environment ready.
exit /b 0
