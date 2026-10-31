@echo off
setlocal
cd /d "%~dp0"

if not exist "vendor\wimlib-imagex.exe" (
  echo Missing vendor\wimlib-imagex.exe
  exit /b 1
)
if not exist "vendor\libwim-15.dll" (
  echo Missing vendor\libwim-15.dll
  exit /b 1
)

python -m PyInstaller --noconfirm --clean --onefile --console --name MumxPack ^
  --add-binary "%~dp0vendor\wimlib-imagex.exe;." ^
  --add-binary "%~dp0vendor\libwim-15.dll;." ^
  --distpath dist --workpath build --specpath build MumxPack.py

if errorlevel 1 exit /b %errorlevel%
echo Built: dist\MumxPack.exe
