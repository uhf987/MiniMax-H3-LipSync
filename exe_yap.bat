@echo off
chcp 65001 >nul
pip install flask requests pywebview pyinstaller
pyinstaller --noconfirm --onefile --windowed --name LipSyncRender sarki_arayuz.py
echo.
echo Bitti. EXE: dist\LipSyncRender.exe
pause
