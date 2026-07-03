@echo off
:loop
php C:\xampp82\htdocs\Dvibes\process_queue.php

timeout /t 2 >nul
goto loop