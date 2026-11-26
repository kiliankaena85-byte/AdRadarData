@echo off
chcp 65001 >nul
title AdRadar LegalSheaf - Панель управления

:MENU
cls
echo ===============================================================================
echo                   ADRADAR: АВТОНОМНЫЙ ЮРИДИЧЕСКИЙ КОМПЛАЕНС                    
echo ===============================================================================
echo.
echo   [1] Экспресс-аудит сайта и Visual QC (или Enter для пакетного --from-hunt)
echo   [2] Боевой сбор лидов с VC.ru (поиск, упаковка КП, реестр 90д)
echo   [3] Открыть Visual QC Review Desk в браузере (сводный или по домену)
echo   [4] Импорт вердиктов Review Desk в обучение (feedback_*.json)
echo   [5] Собрать Комплект устранения нарушений (14 900 ₽) для клиента
echo   [6] Реестр контактов и защита от спама (кулдаун 90 дней / Opt-Out)
echo   [7] Доктор системы и проверка живых канареек РКН и ФНС
echo   [8] Выход
echo.
echo ===============================================================================
set /p OPT="Выберите пункт меню [1-8]: "

if "%OPT%"=="1" goto AUDIT_SITE
if "%OPT%"=="2" goto HUNT_VCRU
if "%OPT%"=="3" goto OPEN_DESK
if "%OPT%"=="4" goto LEARN_DESK
if "%OPT%"=="5" goto BUILD_KIT
if "%OPT%"=="6" goto MANAGE_LEDGER
if "%OPT%"=="7" goto SYSTEM_DOCTOR
if "%OPT%"=="8" goto EXIT_PROG
if "%OPT%"=="0" goto EXIT_PROG

echo.
echo Неверный выбор! Пожалуйста, выберите число от 1 до 8.
timeout /t 2 >nul
goto MENU

:AUDIT_SITE
cls
echo ===============================================================================
echo          [1] ЭКСПРЕСС-АУДИТ САЙТА И ВИЗУАЛЬНОЕ ОТК (PLAYWRIGHT + VLM)
echo ===============================================================================
echo.
set "DOMAIN="
set /p DOMAIN="Введите домен (или Enter для пакетного Visual QC по --from-hunt): "
echo.
if "%DOMAIN%"=="" (
    echo [*] Запуск пакетного Visual QC по необработанным лидам VC.ru [--from-hunt]...
    .venv\Scripts\python.exe scripts\run_visual_qc.py --from-hunt
) else (
    echo [*] Запуск аудита сайта и захвата триггерных зон в Chromium...
    .venv\Scripts\python.exe scripts\run_visual_qc.py --domain %DOMAIN%
)
echo.
echo Нажмите любую клавишу для возврата в меню...
pause >nul
goto MENU

:HUNT_VCRU
cls
echo ===============================================================================
echo                   [2] БОЕВОЙ СБОР ПУБЛИКАЦИЙ И ЛИДОВ С VC.RU                   
echo ===============================================================================
echo.
set /p LIMIT="Введите количество статей для сбора (по умолчанию 30): "
if "%LIMIT%"=="" set LIMIT=30
echo.
echo [*] Сбор публикаций и упаковка лидов (Лимит: %LIMIT%)...
.venv\Scripts\adradar.exe hunt-vcru --limit %LIMIT%
echo.
echo Нажмите любую клавишу для возврата в меню...
pause >nul
goto MENU

:OPEN_DESK
cls
echo ===============================================================================
echo               [3] ОТКРЫТИЕ ИНТЕРФЕЙСА VISUAL QC REVIEW DESK                   
echo ===============================================================================
echo.
set "DESK_DOMAIN="
set /p DESK_DOMAIN="Введите домен (или Enter для сводного / последнего Пульта): "
if not "%DESK_DOMAIN%"=="" (
    set "DESK_SLUG=%DESK_DOMAIN:https://=%"
    set "DESK_SLUG=%DESK_SLUG:http://=%"
    set "DESK_SLUG=%DESK_SLUG:www.=%"
    set "DESK_SLUG=%DESK_SLUG:/=_%"
    set "DESK_SLUG=%DESK_SLUG:.=_%"
    if exist "C:\AdRadarData\review_desk\%DESK_SLUG%\index.html" (
        start "" "C:\AdRadarData\review_desk\%DESK_SLUG%\index.html"
    ) else (
        echo [!] Папка C:\AdRadarData\review_desk\%DESK_SLUG% не найдена.
    )
) else (
    if exist "C:\AdRadarData\review_desk\index.html" (
        echo [*] Открытие сводного индекса пакетного ОТК: C:\AdRadarData\review_desk\index.html
        start "" "C:\AdRadarData\review_desk\index.html"
        goto AFTER_DESK
    )
    set FOUND_DESK=
    for /f "tokens=*" %%i in ('dir /b /ad /o-d "C:\AdRadarData\review_desk" 2^>nul') do (
        if exist "C:\AdRadarData\review_desk\%%i\index.html" (
            echo [*] Открытие последнего аудита: %%i
            start "" "C:\AdRadarData\review_desk\%%i\index.html"
            set FOUND_DESK=1
            goto AFTER_DESK
        )
    )
    if not defined FOUND_DESK (
        echo [!] Не найдено сгенерированных отчетов в C:\AdRadarData\review_desk
    )
)
:AFTER_DESK
echo.
echo Нажмите любую клавишу для возврата в меню...
pause >nul
goto MENU

:LEARN_DESK
cls
echo ===============================================================================
echo          [4] ИМПОРТ ВЕРДИКТОВ REVIEW DESK В КАЛИБРОВКУ (--from-desk)
echo ===============================================================================
echo.
set "FB_PATH="
set /p FB_PATH="Введите полный путь к файлу feedback_*.json: "
if "%FB_PATH%"=="" goto MENU
echo.
echo [*] Загрузка вердиктов и Hard-Negative сэмплов в память AdRadar...
.venv\Scripts\adradar.exe learn --from-desk "%FB_PATH%"
echo.
echo Нажмите любую клавишу для возврата в меню...
pause >nul
goto MENU

:BUILD_KIT
cls
echo ===============================================================================
echo            [5] СБОРКА КОМПЛЕКТА УСТРАНЕНИЯ НАРУШЕНИЙ (14 900 ₽)               
echo ===============================================================================
echo.
set /p KIT_DOMAIN="Введите домен клиента для упаковки комплекта: "
if "%KIT_DOMAIN%"=="" goto MENU
set "BRAND_NAME="
set /p BRAND_NAME="Введите бренд или агентство для White-Label (опционально): "
echo.
echo [*] Формирование документов комплекта под реквизиты клиента...
if "%BRAND_NAME%"=="" (
    .venv\Scripts\adradar.exe build-kit --domain %KIT_DOMAIN%
) else (
    .venv\Scripts\adradar.exe build-kit --domain %KIT_DOMAIN% --white-label "%BRAND_NAME%"
)
echo.
echo Нажмите любую клавишу для возврата в меню...
pause >nul
goto MENU

:MANAGE_LEDGER
cls
echo ===============================================================================
echo                [6] РЕЕСТР КОНТАКТОВ И ЗАЩИТА ОТ СПАМА                         
echo ===============================================================================
echo.
echo   [1] Просмотреть реестр компаний и оставшиеся дни кулдауна
echo   [2] Отметить отправку письма клиенту (активировать кулдаун 90 дней)
echo   [3] Внести компанию в черный список отказов (Opt-Out навсегда)
echo   [4] Назад в главное меню
echo.
set /p L_ACT="Выберите действие [1-4]: "

if "%L_ACT%"=="1" (
    echo.
    .venv\Scripts\adradar.exe ledger --list
    echo.
    pause
    goto MANAGE_LEDGER
)
if "%L_ACT%"=="2" (
    echo.
    set /p SENT_DOM="Введите домен, которому отправлено письмо: "
    if not "%SENT_DOM%"=="" (
        .venv\Scripts\adradar.exe ledger --mark-sent %SENT_DOM%
    )
    echo.
    pause
    goto MANAGE_LEDGER
)
if "%L_ACT%"=="3" (
    echo.
    set /p OPT_DOM="Введите домен, запросивший отказ от рассылок: "
    if not "%OPT_DOM%"=="" (
        .venv\Scripts\adradar.exe ledger --mark-opt-out %OPT_DOM%
    )
    echo.
    pause
    goto MANAGE_LEDGER
)
goto MENU

:SYSTEM_DOCTOR
cls
echo ===============================================================================
echo              [7] ДОКТОР СИСТЕМЫ И ПРОВЕРКА КАНАРЕЕК РКН И ФНС                 
echo ===============================================================================
echo.
echo [*] Выполнение комплексной диагностики инфраструктуры AdRadar...
.venv\Scripts\adradar.exe doctor --live-canaries
echo.
echo Нажмите любую клавишу для возврата в меню...
pause >nul
goto MENU

:EXIT_PROG
echo.
echo Выход из AdRadar. Хорошего рабочего дня!
exit /b 0
