@echo off
:: Launches AV1 Queue Studio as a background tray-icon process — no console
:: window, not shown on the taskbar. This window closes itself immediately;
:: look for the icon in the system tray (right-click it for options).
set ROOT_DIR=%~dp0
wscript.exe "%ROOT_DIR%scripts\launch_hidden.vbs" "%ROOT_DIR%scripts\tray_launcher.ps1"
exit
