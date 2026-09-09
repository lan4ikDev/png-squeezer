@echo off
rem Double-click launcher for PNG Squeezer.
rem Installs the dependencies on the first run, then opens the window.
chcp 65001 >nul

setlocal
cd /d "%~dp0"

set PY=py -3
%PY% --version >nul 2>&1
if errorlevel 1 set PY=python

%PY% -c "import PIL, numpy, imagequant, oxipng, tkinterdnd2" >nul 2>&1
if errorlevel 1 (
    echo Ставлю зависимости, это займёт минуту...
    %PY% -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Не удалось установить зависимости. Запустите вручную:
        echo     %PY% -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

rem pythonw keeps the console window from appearing behind the app.
set PYW=pyw -3
%PYW% --version >nul 2>&1
if errorlevel 1 set PYW=%PY%

start "" %PYW% -m png_squeezer
endlocal
