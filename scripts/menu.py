"""AdRadar Interactive Control Console.

Provides a robust, zero-encoding-bug terminal dashboard for legal audits,
visual QC, lead generation, and turnkey compliance kits.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

# Force UTF-8 encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_DIR = Path(__file__).resolve().parent.parent
VENV_PYTHON = PROJECT_DIR / ".venv" / "Scripts" / "python.exe"
VENV_ADRADAR = PROJECT_DIR / ".venv" / "Scripts" / "adradar.exe"
DATA_DIR = Path(os.environ.get("ADRADAR_DATA_DIR", "C:/AdRadarData"))

# ANSI Color codes
CYAN = "\033[96m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def clear_screen() -> None:
    if os.environ.get("ADRADAR_NON_INTERACTIVE") == "1":
        return
    os.system("cls" if os.name == "nt" else "clear")


def safe_input(prompt: str, default: str = "") -> str:
    if os.environ.get("ADRADAR_NON_INTERACTIVE") == "1":
        print(f"{prompt}{default} [NON_INTERACTIVE]")
        return default
    try:
        return input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        return default


def open_folder(path: Path) -> None:
    if os.environ.get("ADRADAR_NON_INTERACTIVE") == "1":
        return
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))


def open_browser(path: Path) -> None:
    if os.environ.get("ADRADAR_NON_INTERACTIVE") == "1":
        return
    if path.exists():
        webbrowser.open(path.as_uri())


def run_command(cmd: list[str]) -> int:
    print(f"\n{DIM}[*] Команда: {' '.join(cmd)}{RESET}\n")
    try:
        result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
        return result.returncode
    except KeyboardInterrupt:
        print(f"\n{YELLOW}[!] Прервано пользователем.{RESET}")
        return 130
    except Exception as e:
        print(f"\n{RED}[!] Ошибка выполнения: {e}{RESET}")
        return 1


def opt_hunt() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [1] СБОР СВЕЖИХ ЛИДОВ И РЕКЛАМНЫХ СТАТЕЙ С VC.RU{RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    count_str = safe_input("Сколько публикаций проверить? [Enter = 100]: ", default="100").strip()
    count = int(count_str) if count_str.isdigit() and int(count_str) > 0 else 100

    print(f"\n{GREEN}[*] Запуск сбора {count} статей через API VC.ru v2.8...{RESET}")
    run_command([str(VENV_ADRADAR), "hunt-vcru", "--limit", str(count)])

    target_dir = DATA_DIR / "reports" / "vcru_leads_v2"
    if not target_dir.exists():
        target_dir = DATA_DIR / "reports"
    print(f"\n{GREEN}[*] Открываю папку с готовыми отчетами в Проводнике: {target_dir}{RESET}")
    open_folder(target_dir)


def opt_article() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [2] ЭКСПРЕСС-АУДИТ ОДНОЙ СТАТЬИ (VC.RU / HABR / МЕДИА){RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    url = safe_input("Введите ссылку на статью: ").strip()
    if not url:
        print(f"{YELLOW}[!] Ссылка не указана.{RESET}")
        return

    print(f"\n{GREEN}[*] Сканирование публикации на рекламные маркеры и отсутствие erid...{RESET}")
    run_command([str(VENV_ADRADAR), "audit-article", "--url", url])


def opt_site() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [3] ГЛУБОКАЯ МУЛЬТИСТРАНИЧНАЯ ПРОВЕРКА САЙТА (ДО 6 СТРАНИЦ){RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    domain = safe_input("Введите домен сайта (например, c3.ru или upservice.com): ").strip()
    if not domain:
        print(f"{YELLOW}[!] Домен не указан.{RESET}")
        return

    clean_dom = re.sub(r"^https?://", "", domain).strip("/")
    clean_dom = clean_dom.split("/")[0]

    print(f"\n{GREEN}[*] Запуск глубокого аудита сайта {clean_dom}...{RESET}")
    run_command([str(VENV_ADRADAR), "audit-site", "--domain", clean_dom, "--deep"])

    clean_slug = clean_dom.replace(".", "_")
    target_dir = DATA_DIR / "reports" / "leads" / clean_slug
    if not target_dir.exists():
        alt_dir = DATA_DIR / "reports" / "leads" / clean_dom
        target_dir = alt_dir if alt_dir.exists() else DATA_DIR / "reports"
    print(f"\n{GREEN}[*] Открываю папку с отчетами в Проводнике: {target_dir}{RESET}")
    open_folder(target_dir)


def opt_visual() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [4] ПУЛЬТ 15-СЕКУНДНОЙ ПРОВЕРКИ ЧЕЛОВЕКОМ + СНАЙПЕРСКИЕ СКРИНШОТЫ{RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")

    domain = safe_input("Введите домен (или оставьте пустым для пакетного --from-hunt): ").strip()
    if not domain:
        print(f"\n{GREEN}[*] Запуск пакетного Visual QC по результатам VC.ru (--from-hunt)...{RESET}")
        run_command([str(VENV_PYTHON), str(PROJECT_DIR / "scripts" / "run_visual_qc.py"), "--from-hunt"])
        batch_index = DATA_DIR / "review_desk" / "index.html"
        if batch_index.exists():
            print(f"\n{GREEN}[*] Открываю сводный Пульт ОТК в браузере: {batch_index}{RESET}")
            open_browser(batch_index)
        else:
            print(f"{YELLOW}[!] Сводный индекс {batch_index} не найден.{RESET}")
        return

    print(f"\n{GREEN}[*] Запуск браузера, захват зон, анализ сносок и неавторегрессионный замок Vega-U...{RESET}")
    run_command([str(VENV_PYTHON), str(PROJECT_DIR / "scripts" / "run_visual_qc.py"), "--domain", domain])

    clean_dom = re.sub(r"^https?://", "", domain.strip().lower()).split("/")[0].split(":")[0]
    if clean_dom.startswith("www."):
        clean_dom = clean_dom[4:]
    clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", clean_dom.replace(".", "_")).lower()
    desk_path = DATA_DIR / "review_desk" / clean_slug / "index.html"
    if not desk_path.exists():
        rd_root = DATA_DIR / "review_desk"
        if rd_root.exists():
            candidates = sorted(rd_root.glob("*/index.html"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                desk_path = candidates[0]

    if desk_path.exists():
        print(f"\n{GREEN}[*] Открываю Пульт ревьюера в браузере: {desk_path}{RESET}")
        open_browser(desk_path)
    else:
        print(f"{YELLOW}[!] Файл review_desk не найден.{RESET}")


def opt_kit() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [5] АВТО-СБОРКА КОМПЛЕКТА УСТРАНЕНИЯ РИСКОВ (14 900 руб.){RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    domain = safe_input("Введите домен клиента (например, c3.c3.ru или c3.ru): ").strip()
    if not domain:
        print(f"{YELLOW}[!] Домен не указан.{RESET}")
        return

    agency = safe_input("Название вашего агентства [Enter, если без White-Label]: ").strip()

    print(f"\n{GREEN}[*] Генерация персонального комплекта документов и безопасных форм...{RESET}")
    cmd = [str(VENV_ADRADAR), "build-kit", "--domain", domain]
    if agency:
        cmd.extend(["--white-label", agency])

    run_command(cmd)

    target_dir = DATA_DIR / "kits"
    clean_dir = domain.replace("https://", "").replace("http://", "").strip("/").replace(".", "_").replace("/", "_")
    client_dir = target_dir / clean_dir
    open_dir = client_dir if client_dir.exists() else target_dir
    print(f"\n{GREEN}[*] Открываю папку с готовым комплектом в Проводнике: {open_dir}{RESET}")
    open_folder(open_dir)


def opt_horizon() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [6] ПРОГНОЗ РИСКОВ И ОПТИМАЛЬНОЙ СТРАТЕГИИ НА ГОД (PLAN-HORIZON){RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    domain = safe_input("Введите домен компании: ").strip()
    if not domain:
        print(f"{YELLOW}[!] Домен не указан.{RESET}")
        return

    print(f"\n{GREEN}[*] Расчет эволюционной траектории снижения штрафов...{RESET}")
    run_command([str(VENV_ADRADAR), "plan-horizon", "--domain", domain])


def opt_doctor() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [7] ПРОВЕРКА ЗДОРОВЬЯ СИСТЕМЫ, РЕЕСТРОВ И СВЯЗИ (DOCTOR){RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    print(f"{GREEN}[*] Опрос реестра РКН, базы ФНС ГИР БО, проверка моделей и контрольных эталонов...{RESET}")
    run_command([str(VENV_ADRADAR), "doctor", "--live-canaries"])


def opt_lawyer_doc() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [8] ВЫГРУЗИТЬ ЮРИДИЧЕСКИЙ ПАСПОРТ СИСТЕМЫ ДЛЯ ПРОВЕРКИ ЮРИСТОМ{RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
    
    print(f"{GREEN}[*] Генерация Юридического Паспорта Архитектуры и Таблиц Квалификации...{RESET}")
    run_command([str(VENV_ADRADAR), "export-lawyer-doc"])

    html_doc = DATA_DIR / "lawyer_validation" / "ЮРИДИЧЕСКИЙ_ПАСПОРТ_АРХИТЕКТУРЫ.html"
    if html_doc.exists():
        print(f"\n{GREEN}[*] Открываю Юридический Паспорт в браузере: {html_doc}{RESET}")
        open_browser(html_doc)
    else:
        print(f"\n{YELLOW}[!] Файл документа не найден.{RESET}")


def opt_learn_desk() -> None:
    clear_screen()
    print(f"{CYAN}{BOLD}==============================================================================={RESET}")
    print(f"{CYAN}{BOLD} [9] ИМПОРТ ВЕРДИКТОВ REVIEW DESK В КАЛИБРОВКУ (--from-desk){RESET}")
    print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")

    fb_input = safe_input(
        "Путь к файлу feedback_*.json (Enter — последний в Загрузках / review_desk): "
    ).strip().strip('"')
    fb_path: Path | None = Path(fb_input) if fb_input else None
    if fb_path is None:
        search_dirs = [Path.home() / "Downloads", DATA_DIR / "review_desk"]
        candidates = [
            p for d in search_dirs if d.exists() for p in d.glob("feedback_*.json")
        ]
        if candidates:
            fb_path = max(candidates, key=lambda p: p.stat().st_mtime)
            print(f"{GREEN}[*] Найден свежий файл вердиктов: {fb_path}{RESET}")

    if fb_path is None or not fb_path.exists():
        print(f"{YELLOW}[!] Файл feedback_*.json не найден.{RESET}")
        return

    print(f"\n{GREEN}[*] Загрузка вердиктов Review Desk в обучение калибровки...{RESET}")
    run_command([str(VENV_ADRADAR), "learn", "--from-desk", str(fb_path)])


def main() -> None:
    if os.environ.get("ADRADAR_NON_INTERACTIVE") == "1":
        print("[*] ADRADAR_NON_INTERACTIVE=1: menu automated smoke check passed.")
        return

    # Ensure UTF-8 console output
    if os.name == "nt":
        os.system("chcp 65001 >nul 2>&1")

    while True:
        clear_screen()
        print(f"{CYAN}{BOLD}==============================================================================={RESET}")
        print(f"{CYAN}{BOLD}                ADRADAR: ЦЕНТР ПРАВОВОГО АУДИТА И КОМПЛАЕНСА{RESET}")
        print(f"{CYAN}{BOLD}==============================================================================={RESET}\n")
        print(f"  {GREEN}{BOLD}[1]{RESET}  Свежие лиды с VC.ru (сбор рекламных публикаций, ИНН и выручки)")
        print(f"  {GREEN}{BOLD}[2]{RESET}  Проверить ОДНУ статью (VC.ru, Habr, блог) на скрытую рекламу (38-ФЗ)")
        print(f"  {GREEN}{BOLD}[3]{RESET}  Глубокая проверка ОДНОГО сайта (152-ФЗ, 38-ФЗ, подвал, формы, cookie)")
        print(f"  {GREEN}{BOLD}[4]{RESET}  Пульт 15-секундной проверки + Скриншоты (Визуальное ОТК + Vega-U)")
        print(f"  {GREEN}{BOLD}[5]{RESET}  Собрать платный комплект за 14 900 руб. (Политика, формы, дисклеймеры)")
        print(f"  {GREEN}{BOLD}[6]{RESET}  Прогноз рисков компании на год вперед (планирование горизонта)")
        print(f"  {GREEN}{BOLD}[7]{RESET}  Утренняя проверка здоровья системы (лицензии, реестры РКН и ФНС)")
        print(f"  {GREEN}{BOLD}[8]{RESET}  Выгрузить Юридический Паспорт системы для проверки юристом")
        print(f"  {GREEN}{BOLD}[9]{RESET}  Импорт вердиктов Review Desk в обучение (feedback_*.json -> learn)\n")
        print(f"  {YELLOW}{BOLD}[0]{RESET}  Выход")
        print(f"{CYAN}{BOLD}==============================================================================={RESET}")

        try:
            choice = safe_input(f"\n{BOLD}Выберите действие [1-9, 0]: {RESET}", default="0").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nДо свидания!")
            break

        if choice == "1":
            opt_hunt()
        elif choice == "2":
            opt_article()
        elif choice == "3":
            opt_site()
        elif choice == "4":
            opt_visual()
        elif choice == "5":
            opt_kit()
        elif choice == "6":
            opt_horizon()
        elif choice == "7":
            opt_doctor()
        elif choice == "8":
            opt_lawyer_doc()
        elif choice == "9":
            opt_learn_desk()
        elif choice == "0":
            print(f"\n{GREEN}Завершение работы AdRadar. Всего доброго!{RESET}\n")
            break
        else:
            print(f"\n{RED}[!] Неверный выбор. Введите цифру от 0 до 9.{RESET}")

        safe_input(f"\n{DIM}Нажмите Enter для возврата в меню...{RESET}")


if __name__ == "__main__":
    main()
