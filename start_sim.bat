@echo off
rem run mini-scope against the MPLAB X simulator (no hardware)
rem 1. sets up the Python environment on the first start
rem 2. builds test.X, configuration "sim" (only if something changed)
rem 3. starts the GUI: port "MDB" -> Connect (40-60 s, progress bar) -> Load
setlocal
call "%~dp0tools\setup_env.bat" || (pause & exit /b 1)
if "%MINISCOPE_VENV%"=="" set MINISCOPE_VENV=%~dp0.venv
set MPLABX=C:\Program Files\Microchip\MPLABX\v6.35
set PATH=%MPLABX%\gnuBins\GnuWin32\bin;%PATH%

pushd "%~dp0test.X"
rem in a fresh clone the MPLAB makefiles do not exist yet: generate them once
if not exist "nbproject\Makefile-sim.mk" (
    echo Generating MPLAB makefiles ...
    call "%MPLABX%\mplab_platform\bin\prjMakefilesGenerator.bat" . >build_sim.log 2>&1
)
make -f Makefile CONF=sim build >>build_sim.log 2>&1
if errorlevel 1 (
    echo Build failed, see test.X\build_sim.log
    popd
    pause
    exit /b 1
)
popd

"%MINISCOPE_VENV%\Scripts\python.exe" "%~dp0gui\scope_gui.py" --mdb "%~dp0test.X\dist\sim\production\test.X.production.elf" %*
