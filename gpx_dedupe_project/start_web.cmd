@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 goto no_uv

uv run --offline --no-project --python 3.14 --index-url https://pypi.tuna.tsinghua.edu.cn/simple --with numpy --with scipy --with pyproj --with matplotlib python -c "import numpy, scipy, pyproj, matplotlib" >nul 2>nul
if errorlevel 1 goto online

echo Open http://127.0.0.1:8765/ in your browser. Press Ctrl+C to stop.
uv run --offline --no-project --python 3.14 --index-url https://pypi.tuna.tsinghua.edu.cn/simple --with numpy --with scipy --with pyproj --with matplotlib python web_app.py
goto finished

:online
echo Preparing Python dependencies. This first launch needs internet access.
echo Open http://127.0.0.1:8765/ after the server starts. Press Ctrl+C to stop.
uv run --no-project --python 3.14 --index-url https://pypi.tuna.tsinghua.edu.cn/simple --with numpy --with scipy --with pyproj --with matplotlib python web_app.py
goto finished

:no_uv
py -3.14 -c "import numpy, scipy, pyproj" >nul 2>nul
if errorlevel 1 goto manual
echo Open http://127.0.0.1:8765/ in your browser. Press Ctrl+C to stop.
py -3.14 web_app.py
goto finished

:manual
echo Install dependencies first, then start the server:
echo   python -m pip install -r requirements.txt
echo   python web_app.py
echo Or install uv and launch this file again.
pause
exit /b 1

:finished
if errorlevel 1 pause
endlocal
