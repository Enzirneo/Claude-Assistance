@echo off
cd /d "%~dp0"
python -m pip install --quiet pyinstaller
python -m PyInstaller --noconfirm --onefile --noconsole --name ccwatch --icon "%~dp0icon.ico" --add-data "%~dp0web;web" --distpath . --workpath build --specpath build server.py
if errorlevel 1 (
  echo.
  echo FALHOU - veja a saida acima.
  exit /b 1
)
echo.
echo Pronto: ccwatch.exe
