@echo off
cd /d "%~dp0"
echo ===============================================
echo  Sims 4 Dynamic Lot Main Menu - Update ^& Launch
echo ===============================================
echo.
DynamicLotMainMenu.exe --launch
set ERR=%ERRORLEVEL%
echo.
if not "%ERR%"=="0" (
  echo Update failed. Error code: %ERR%
) else (
  echo Update completed successfully.
)
echo.
pause
exit /b %ERR%
