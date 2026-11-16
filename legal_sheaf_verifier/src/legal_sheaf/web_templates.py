"""
Web Templates and Fintech Showcase UI for AdRadar (Russian Engineering Fintech Style).
Dogfooding 152-FZ and 168-FZ:
- 100% inline CSS and vanilla JS (0 external CDNs, 0 Google Fonts).
- Russian language UI and typography.
- Unchecked consent checkboxes by default.
- Bento Grid and X-Ray diagnostics design.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


def render_landing_page() -> str:
    """
    Renders main public landing page for AdRadar (Russian Engineering Fintech).
    """
    year = datetime.now().year
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AdRadar — Автономный правовой аудит и радар комплаенса сайтов (152-ФЗ, 38-ФЗ)</title>
<meta name="description" content="Инженерный сканер сайтов и публикаций по 152-ФЗ, 38-ФЗ и КоАП РФ. Устранение рисков штрафов и проверок РКН/ФАС.">
<style>
:root {{
    --bg-main: #f8fafc;
    --bg-card: #ffffff;
    --dark-xray: #0f172a;
    --dark-surface: #1e293b;
    --text-primary: #0f172a;
    --text-muted: #64748b;
    --text-light: #f8fafc;
    --accent: #2563eb;
    --accent-hover: #1d4ed8;
    --border: #e2e8f0;
    --border-dark: #334155;
    --red: #ef4444;
    --green: #10b981;
    --amber: #f59e0b;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: var(--font);
    background-color: var(--bg-main);
    color: var(--text-primary);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
}}
.container {{
    max-width: 1200px;
    margin: 0 auto;
    padding: 0 24px;
}}
header {{
    background: #ffffff;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 0;
    z-index: 100;
}}
.nav-bar {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: 72px;
}}
.logo {{
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: var(--dark-xray);
    display: flex;
    align-items: center;
    gap: 8px;
    text-decoration: none;
}}
.logo-badge {{
    background: var(--dark-xray);
    color: #38bdf8;
    font-size: 11px;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 4px;
    letter-spacing: 0.5px;
}}
.nav-links {{
    display: flex;
    gap: 24px;
    align-items: center;
}}
.nav-links a {{
    color: var(--text-muted);
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    transition: color 0.2s;
}}
.nav-links a:hover {{ color: var(--text-primary); }}
.btn {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 12px 24px;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    border: none;
    transition: all 0.2s ease;
    text-decoration: none;
}}
.btn-primary {{
    background: var(--accent);
    color: #ffffff;
}}
.btn-primary:hover {{ background: var(--accent-hover); }}
.btn-dark {{
    background: var(--dark-xray);
    color: #ffffff;
}}
.btn-dark:hover {{ background: #1e293b; }}
.btn-outline {{
    background: transparent;
    color: var(--dark-xray);
    border: 1px solid var(--border);
}}
.btn-outline:hover {{ background: #f1f5f9; }}

/* Hero Section & X-Ray Chamber */
.hero {{
    padding: 64px 0 48px;
}}
.hero-grid {{
    display: grid;
    grid-template-columns: 1.2fr 0.8fr;
    gap: 32px;
    align-items: stretch;
}}
@media (max-width: 900px) {{
    .hero-grid {{ grid-template-columns: 1fr; }}
}}
.xray-chamber {{
    background: var(--dark-xray);
    color: var(--text-light);
    border-radius: 12px;
    padding: 36px;
    box-shadow: 0 20px 25px -5px rgba(15, 23, 42, 0.3), 0 8px 10px -6px rgba(15, 23, 42, 0.3);
    border: 1px solid var(--border-dark);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}}
.xray-header {{
    margin-bottom: 24px;
}}
.xray-badge {{
    display: inline-block;
    background: rgba(56, 189, 248, 0.15);
    color: #38bdf8;
    border: 1px solid rgba(56, 189, 248, 0.3);
    padding: 4px 10px;
    border-radius: 9999px;
    font-size: 12px;
    font-weight: 600;
    margin-bottom: 16px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}
.xray-title {{
    font-size: 32px;
    font-weight: 800;
    line-height: 1.2;
    margin-bottom: 12px;
    color: #ffffff;
}}
.xray-subtitle {{
    font-size: 15px;
    color: #94a3b8;
    line-height: 1.5;
}}
.scan-box {{
    background: var(--dark-surface);
    border: 1px solid var(--border-dark);
    border-radius: 8px;
    padding: 16px;
    margin: 24px 0;
}}
.scan-form {{
    display: flex;
    gap: 12px;
}}
@media (max-width: 600px) {{
    .scan-form {{ flex-direction: column; }}
}}
.scan-input {{
    flex: 1;
    background: #0f172a;
    border: 1px solid var(--border-dark);
    color: #ffffff;
    padding: 14px 16px;
    border-radius: 6px;
    font-size: 15px;
    outline: none;
    font-family: inherit;
}}
.scan-input:focus {{ border-color: #38bdf8; }}
.scan-result {{
    display: none;
    margin-top: 16px;
    padding: 16px;
    background: #0b1120;
    border-radius: 6px;
    border-left: 4px solid var(--amber);
    font-size: 13px;
}}

/* Manifesto Card */
.manifesto-card {{
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 36px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}}
.manifesto-title {{
    font-size: 20px;
    font-weight: 700;
    color: var(--dark-xray);
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 8px;
}}
.manifesto-list {{
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin-bottom: 24px;
}}
.manifesto-item {{
    display: flex;
    gap: 12px;
    font-size: 14px;
}}
.manifesto-item strong {{
    color: var(--red);
    display: block;
    margin-bottom: 2px;
}}
.manifesto-icon {{
    font-size: 18px;
    flex-shrink: 0;
}}

/* Bento Grid: Tariffs */
.section {{
    padding: 64px 0;
}}
.section-header {{
    text-align: center;
    max-width: 700px;
    margin: 0 auto 48px;
}}
.section-badge {{
    color: var(--accent);
    font-weight: 700;
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-bottom: 8px;
}}
.section-title {{
    font-size: 32px;
    font-weight: 800;
    color: var(--dark-xray);
    margin-bottom: 12px;
}}
.section-desc {{
    font-size: 16px;
    color: var(--text-muted);
}}
.tariffs-grid {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 24px;
}}
@media (max-width: 1024px) {{
    .tariffs-grid {{ grid-template-columns: repeat(2, 1fr); }}
}}
@media (max-width: 600px) {{
    .tariffs-grid {{ grid-template-columns: 1fr; }}
}}
.tariff-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 28px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    transition: transform 0.2s, box-shadow 0.2s;
    position: relative;
}}
.tariff-card:hover {{
    transform: translateY(-4px);
    box-shadow: 0 12px 20px -5px rgba(0, 0, 0, 0.08);
}}
.tariff-card.featured {{
    border-color: var(--accent);
    box-shadow: 0 10px 15px -3px rgba(37, 99, 235, 0.1);
}}
.tariff-badge {{
    position: absolute;
    top: -12px;
    right: 20px;
    background: var(--accent);
    color: #ffffff;
    font-size: 11px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 9999px;
    text-transform: uppercase;
}}
.tariff-name {{
    font-size: 18px;
    font-weight: 700;
    margin-bottom: 8px;
}}
.tariff-price {{
    font-size: 32px;
    font-weight: 800;
    color: var(--dark-xray);
    margin-bottom: 16px;
}}
.tariff-price span {{
    font-size: 14px;
    font-weight: 500;
    color: var(--text-muted);
}}
.tariff-features {{
    list-style: none;
    font-size: 13px;
    color: #334155;
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 24px;
    flex-grow: 1;
}}
.tariff-features li {{
    display: flex;
    gap: 8px;
    align-items: flex-start;
}}
.tariff-features li::before {{
    content: "✓";
    color: var(--green);
    font-weight: 700;
}}

/* Bento Grid: Cases */
.cases-grid {{
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 24px;
}}
@media (max-width: 768px) {{
    .cases-grid {{ grid-template-columns: 1fr; }}
}}
.case-card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 28px;
}}
.case-badge {{
    font-size: 12px;
    font-weight: 700;
    color: var(--accent);
    text-transform: uppercase;
    margin-bottom: 8px;
}}
.case-metric {{
    font-size: 28px;
    font-weight: 800;
    color: var(--dark-xray);
    margin-bottom: 8px;
}}
.case-title {{
    font-size: 16px;
    font-weight: 700;
    margin-bottom: 8px;
}}
.case-text {{
    font-size: 14px;
    color: var(--text-muted);
    line-height: 1.5;
}}

/* Lead Capture Form */
.lead-section {{
    background: #ffffff;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 48px;
    margin-bottom: 64px;
}}
.lead-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 40px;
    align-items: center;
}}
@media (max-width: 800px) {{
    .lead-grid {{ grid-template-columns: 1fr; }}
}}
.form-group {{
    margin-bottom: 16px;
}}
.form-label {{
    display: block;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 6px;
    color: var(--dark-xray);
}}
.form-input, .form-select {{
    width: 100%;
    padding: 12px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 14px;
    font-family: inherit;
    background: #ffffff;
}}
.form-input:focus, .form-select:focus {{
    outline: none;
    border-color: var(--accent);
}}
.checkbox-group {{
    display: flex;
    align-items: flex-start;
    gap: 10px;
    margin-top: 16px;
    font-size: 13px;
    color: var(--text-muted);
}}
.checkbox-group input[type="checkbox"] {{
    margin-top: 3px;
}}

footer {{
    background: #ffffff;
    border-top: 1px solid var(--border);
    padding: 32px 0;
    font-size: 13px;
    color: var(--text-muted);
}}
.footer-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 16px;
}}
.footer-links {{
    display: flex;
    gap: 20px;
}}
.footer-links a {{
    color: var(--text-muted);
    text-decoration: none;
}}
.footer-links a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>

<header>
    <div class="container nav-bar">
        <a href="/" class="logo">
            <span>AdRadar</span>
            <span class="logo-badge">ИНЖИНИРИНГ</span>
        </a>
        <div class="nav-links">
            <a href="#xray">Рентген сайта</a>
            <a href="#tariffs">Тарифы</a>
            <a href="#cases">Кейсы</a>
            <a href="/demo">Демо-отчет</a>
            <a href="#order" class="btn btn-primary" style="padding: 8px 16px;">Запустить проверку</a>
        </div>
    </div>
</header>

<main class="container">
    <!-- Hero / X-Ray Chamber -->
    <section class="hero" id="xray">
        <div class="hero-grid">
            <div class="xray-chamber">
                <div class="xray-header">
                    <span class="xray-badge">Контур юридической криминалистики</span>
                    <h1 class="xray-title">Автономный рентген сайта на риски 152-ФЗ и 38-ФЗ</h1>
                    <p class="xray-subtitle">Сканирует сетевой периметр, скрытые трекеры, формы сбора ПД и реестры РКН/ФАС за 12 секунд. Без галлюцинаций.</p>
                </div>

                <div class="scan-box">
                    <form class="scan-form" id="expressForm" onsubmit="runExpressScan(event)">
                        <input type="text" id="scanDomain" class="scan-input" placeholder="Введите домен компании (например: mycompany.ru)" required>
                        <button type="submit" id="scanBtn" class="btn btn-primary">Проверить риски</button>
                    </form>
                    <div id="scanResult" class="scan-result"></div>
                </div>

                <div style="font-size: 12px; color: #64748b; display: flex; gap: 20px;">
                    <span>✓ Без сохранения персональных данных</span>
                    <span>✓ Проверка по базе Приказа № 180 РКН</span>
                    <span>✓ Расчет санкций КоАП без умножения</span>
                </div>
            </div>

            <div class="manifesto-card">
                <div>
                    <h2 class="manifesto-title">Почему ChatGPT здесь бесполезен</h2>
                    <ul class="manifesto-list">
                        <li class="manifesto-item">
                            <span class="manifesto-icon">⚠️</span>
                            <div>
                                <strong>Галлюцинации законов и судебной практики</strong>
                                Языковые модели выдумывают несуществующие номера судебных дел и штрафные статьи.
                            </div>
                        </li>
                        <li class="manifesto-item">
                            <span class="manifesto-icon">👁️</span>
                            <div>
                                <strong>Слепота к DOM-дереву и сетевому периметру</strong>
                                Не видят реальный трафик, IP-адреса хостинга, поддомены и параметры передачи данных в США/ЕС.
                            </div>
                        </li>
                        <li class="manifesto-item">
                            <span class="manifesto-icon">⚖️</span>
                            <div>
                                <strong>Нулевая юридическая ответственность</strong>
                                Не дают готовых документов для госорганов и не учитывают льготы МСП по ст. 4.1.1 КоАП РФ.
                            </div>
                        </li>
                    </ul>
                </div>
                <div style="background: #f1f5f9; padding: 16px; border-radius: 8px; font-size: 13px; color: #334155;">
                    AdRadar использует детерминированное ядро <strong>LegalSheaf</strong> и строгий реестр сертифицированных судебных цитат.
                </div>
            </div>
        </div>
    </section>

    <!-- Tariffs Bento Grid -->
    <section class="section" id="tariffs">
        <div class="section-header">
            <div class="section-badge">Меню решений</div>
            <h2 class="section-title">Прозрачные тарифы без скрытых платежей</h2>
            <p class="section-desc">От разовой экспертизы рекламной публикации до полного пакета приведения сайта в соответствие с законами РФ.</p>
        </div>

        <div class="tariffs-grid">
            <!-- Tariff 1 -->
            <div class="tariff-card">
                <div>
                    <h3 class="tariff-name">Экспресс-аудит статьи</h3>
                    <div class="tariff-price">3 500 ₽ <span>/ разово</span></div>
                    <ul class="tariff-features">
                        <li>Проверка публикации под 38-ФЗ</li>
                        <li>Анализ рекламных токенов erid</li>
                        <li>Связка профиля автора и рекламодателя</li>
                        <li>Иммунитет каталога производителя</li>
                    </ul>
                </div>
                <a href="#order" onclick="selectTariff('Экспресс-аудит статьи (3 500 ₽)')" class="btn btn-outline">Выбрать тариф</a>
            </div>

            <!-- Tariff 2: Featured -->
            <div class="tariff-card featured">
                <span class="tariff-badge">Хит</span>
                <div>
                    <h3 class="tariff-name">Комплект устранения под ключ</h3>
                    <div class="tariff-price">14 900 ₽ <span>/ комплект</span></div>
                    <ul class="tariff-features">
                        <li>Политика конфиденциальности 2026 (152/156-ФЗ)</li>
                        <li>Раздельные формы согласий без пакетирования</li>
                        <li>Чистые HTML/JS сниппеты (0 CDN)</li>
                        <li>ТЗ для разработчиков + уведомление РКН № 180</li>
                        <li>Опция «Внедрение нашими силами»: +15 000 ₽</li>
                    </ul>
                </div>
                <a href="#order" onclick="selectTariff('Комплект под ключ (14 900 ₽)')" class="btn btn-primary">Получить комплект</a>
            </div>

            <!-- Tariff 3 -->
            <div class="tariff-card">
                <div>
                    <h3 class="tariff-name">Непрерывный радар</h3>
                    <div class="tariff-price">4 900 ₽ <span>/ месяц</span></div>
                    <ul class="tariff-features">
                        <li>Еженедельный мониторинг периметра сайта</li>
                        <li>Контроль появления новых виджетов и трекеров</li>
                        <li>Мониторинг реестров операторов РКН</li>
                        <li>Алерты при возникновении риска проверок</li>
                    </ul>
                </div>
                <a href="#order" onclick="selectTariff('Непрерывный радар (4 900 ₽/мес)')" class="btn btn-outline">Подключить радар</a>
            </div>

            <!-- Tariff 4 -->
            <div class="tariff-card">
                <div>
                    <h3 class="tariff-name">White-Label для агентств</h3>
                    <div class="tariff-price">14 900 ₽ <span>/ месяц</span></div>
                    <ul class="tariff-features">
                        <li>Генерация отчетов под брендом агентства</li>
                        <li>Маржинальность до 500% на допродажах</li>
                        <li>Безлимитная проверка сайтов клиентов</li>
                        <li>Готовые коммерческие скрипты для сейлзов</li>
                    </ul>
                </div>
                <a href="#order" onclick="selectTariff('White-Label агентство (14 900 ₽/мес)')" class="btn btn-dark">Для агентств</a>
            </div>
        </div>
    </section>

    <!-- Cases Bento Grid -->
    <section class="section" id="cases">
        <div class="section-header">
            <div class="section-badge">Практика и прецеденты</div>
            <h2 class="section-title">Реальные цифры и результаты клиентов</h2>
            <p class="section-desc">Анонимизированные примеры снижения санкционных рисков и защиты бизнеса от внеплановых проверок.</p>
        </div>

        <div class="cases-grid">
            <div class="case-card">
                <div class="case-badge">EdTech онлайн-школа</div>
                <div class="case-metric">450 000 ₽</div>
                <div class="case-title">Предотвращен штраф за передачу данных через метрики</div>
                <p class="case-text">Автоматический аудит выявил передачу номеров телефонов из формы регистрации в незадекларированные сторонние счетчики. Сгенерирована скорректированная Политика с указанием операторов метрик.</p>
            </div>

            <div class="case-card">
                <div class="case-badge">SaaS B2B платформа</div>
                <div class="case-metric">10 дней</div>
                <div class="case-title">Снят риск внеплановой проверки по ст. 49 248-ФЗ</div>
                <p class="case-text">Устранено расхождение между заявленными целями обработки в реестре РКН и реальным сбором cookie-идентификаторов на поддоменах личного кабинета до истечения срока предостережения.</p>
            </div>

            <div class="case-card">
                <div class="case-badge">E-commerce ритейлер</div>
                <div class="case-metric">100% 156-ФЗ</div>
                <div class="case-title">Реструктуризация согласий без потери конверсии корзины</div>
                <p class="case-text">Внедрены автономные раздельные чекбоксы: обязательное согласие на выполнение договора и опциональное на маркетинговые рассылки. Исключен риск штрафа по ч. 2 ст. 13.11 КоАП РФ.</p>
            </div>

            <div class="case-card">
                <div class="case-badge">Digital-агентство</div>
                <div class="case-metric">+120 000 ₽</div>
                <div class="case-title">Запуск регулярного сервиса аудитов для клиентов</div>
                <p class="case-text">Агентство подключило тариф White-Label и включило юридический аудит сайтов в стандартный onboarding клиентов по контекстной рекламе, обеспечив высокую допродажу.</p>
            </div>
        </div>
    </section>

    <!-- Lead Capture Form -->
    <section class="lead-section" id="order">
        <div class="lead-grid">
            <div>
                <span class="section-badge">Прямая связь</span>
                <h2 style="font-size: 28px; font-weight: 800; margin-bottom: 12px; color: var(--dark-xray);">Заявка на проведение аудита или внедрение комплекта</h2>
                <p style="font-size: 15px; color: var(--text-muted); margin-bottom: 24px;">
                    Оставьте контактные данные — мы свяжемся с вами в течение 30 минут, согласуем объем периметра и сформируем тестовый отчет.
                </p>
                <div style="font-size: 13px; color: #334155;">
                    <p style="margin-bottom: 8px;"><strong>📞 Горячая линия комплаенса:</strong> contact@adradar.ru</p>
                    <p><strong>🔒 Гарантия конфиденциальности:</strong> данные не передаются третьим лицам.</p>
                </div>
            </div>

            <form id="leadForm" onsubmit="submitLead(event)">
                <div class="form-group">
                    <label class="form-label" for="leadDomain">Домен вашего сайта</label>
                    <input type="text" id="leadDomain" class="form-input" placeholder="domain.ru" required>
                </div>
                <div class="form-group">
                    <label class="form-label" for="leadPhone">Номер телефона</label>
                    <input type="tel" id="leadPhone" class="form-input" placeholder="+7 (999) 000-00-00" required>
                </div>
                <div class="form-group">
                    <label class="form-label" for="leadEmail">Рабочая почта</label>
                    <input type="email" id="leadEmail" class="form-input" placeholder="name@company.ru" required>
                </div>
                <div class="form-group">
                    <label class="form-label" for="leadTariff">Интересующее решение</label>
                    <select id="leadTariff" class="form-select">
                        <option value="Комплект под ключ (14 900 ₽)">Комплект устранения рисков под ключ — 14 900 ₽</option>
                        <option value="Комплект + Внедрение (29 900 ₽)">Комплект + Внедрение нашими силами — 29 900 ₽</option>
                        <option value="Экспресс-аудит статьи (3 500 ₽)">Экспресс-аудит статьи — 3 500 ₽</option>
                        <option value="Непрерывный радар (4 900 ₽/мес)">Непрерывный радар — 4 900 ₽ / месяц</option>
                        <option value="White-Label агентство (14 900 ₽/мес)">White-Label для агентств — 14 900 ₽ / месяц</option>
                    </select>
                </div>

                <div class="checkbox-group">
                    <input type="checkbox" id="consent_pd" name="consent_pd" required>
                    <label for="consent_pd">
                        Я даю согласие на обработку персональных данных в соответствии с <a href="/privacy-policy" target="_blank" style="color: var(--accent);">Политикой конфиденциальности</a> и условиями <a href="/consent" target="_blank" style="color: var(--accent);">Согласия</a>.
                    </label>
                </div>

                <button type="submit" id="leadSubmitBtn" class="btn btn-primary" style="width: 100%; margin-top: 20px;">
                    Заказать внедрение
                </button>
                <div id="leadStatus" style="margin-top: 12px; font-size: 13px; text-align: center;"></div>
            </form>
        </div>
    </section>
</main>

<footer>
    <div class="container footer-row">
        <div>
            © {year} AdRadar Правовой Инжиниринг. Автономная система верификации комплаенса.
        </div>
        <div class="footer-links">
            <a href="/privacy-policy">Политика конфиденциальности</a>
            <a href="/consent">Согласие на обработку ПД</a>
            <a href="/demo">Демо-отчет</a>
        </div>
    </div>
</footer>

<script>
function selectTariff(name) {{
    var sel = document.getElementById("leadTariff");
    if (sel) {{
        for (var i = 0; i < sel.options.length; i++) {{
            if (sel.options[i].value === name) {{
                sel.selectedIndex = i;
                break;
            }}
        }}
    }}
}}

async function runExpressScan(e) {{
    e.preventDefault();
    var domainInput = document.getElementById("scanDomain");
    var btn = document.getElementById("scanBtn");
    var resDiv = document.getElementById("scanResult");
    var domain = domainInput.value.trim();
    if (!domain) return;

    btn.disabled = true;
    btn.innerText = "Сканирование...";
    resDiv.style.display = "block";
    resDiv.innerHTML = "<p style='color: #94a3b8;'>Выполняется глубокое зондирование сетевого периметра и реестров...</p>";

    try {{
        var resp = await fetch("/api/express-check", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ domain: domain }})
        }});
        var data = await resp.json();
        if (resp.ok) {{
            resDiv.innerHTML = 
                "<div style='color: #ffffff; font-weight: 700; margin-bottom: 8px;'>Результаты экспресс-диагностики: " + data.domain + "</div>" +
                "<div style='color: #ef4444; margin-bottom: 4px;'>• Риск [RED]: " + (data.critical_finding || "Не обнаружено критических нарушений") + "</div>" +
                "<div style='color: #10b981; margin-bottom: 4px;'>• Факт [GREEN]: " + (data.compliance_fact || "Базовые заголовки в норме") + "</div>" +
                "<div style='color: #38bdf8; margin-bottom: 8px;'>• Прогноз горизонта стабильности: " + (data.risk_horizon_weeks || 15) + " недель (H*)</div>" +
                "<div style='color: #94a3b8; font-size: 12px;'>Скрытых замечаний: " + (data.hidden_findings_count || 0) + ". Для полного аудита выберите тариф ниже.</div>";
            document.getElementById("leadDomain").value = domain;
        }} else {{
            resDiv.innerHTML = "<span style='color: #ef4444;'>Ошибка: " + (data.error || "Не удалось завершить проверку") + "</span>";
        }}
    }} catch (err) {{
        resDiv.innerHTML = "<span style='color: #ef4444;'>Сетевая ошибка при обращении к серверу аудита.</span>";
    }} finally {{
        btn.disabled = false;
        btn.innerText = "Проверить риски";
    }}
}}

async function submitLead(e) {{
    e.preventDefault();
    var btn = document.getElementById("leadSubmitBtn");
    var status = document.getElementById("leadStatus");
    var consent = document.getElementById("consent_pd").checked;
    if (!consent) {{
        status.innerHTML = "<span style='color: #ef4444;'>Для отправки заявки необходимо согласие на обработку ПД.</span>";
        return;
    }}

    var payload = {{
        domain: document.getElementById("leadDomain").value.trim(),
        phone: document.getElementById("leadPhone").value.trim(),
        email: document.getElementById("leadEmail").value.trim(),
        tariff: document.getElementById("leadTariff").value,
        consent_pd: true
    }};

    btn.disabled = true;
    btn.innerText = "Отправка...";

    try {{
        var resp = await fetch("/api/lead", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(payload)
        }});
        var data = await resp.json();
        if (resp.ok) {{
            status.innerHTML = "<span style='color: #10b981; font-weight: 700;'>✓ Заявка успешно принята! Наш специалист свяжется с вами в ближайшее время.</span>";
            document.getElementById("leadForm").reset();
        }} else {{
            status.innerHTML = "<span style='color: #ef4444;'>Ошибка: " + (data.error || "Сбой отправки заявки") + "</span>";
        }}
    }} catch (err) {{
        status.innerHTML = "<span style='color: #ef4444;'>Сетевая ошибка при отправке заявки.</span>";
    }} finally {{
        btn.disabled = false;
        btn.innerText = "Заказать внедрение";
    }}
}}
</script>
</body>
</html>
"""


def render_demo_page() -> str:
    """
    Renders an interactive demonstration audit sheet (/demo) showing the X-Ray capabilities.
    """
    year = datetime.now().year
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Демо-аудит AdRadar — Образец технического отчета</title>
<style>
:root {{
    --bg-main: #f8fafc;
    --dark-xray: #0f172a;
    --dark-surface: #1e293b;
    --text-primary: #0f172a;
    --text-muted: #64748b;
    --accent: #2563eb;
    --border: #e2e8f0;
    --red: #ef4444;
    --green: #10b981;
    --amber: #f59e0b;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: var(--font);
    background-color: var(--bg-main);
    color: var(--text-primary);
    line-height: 1.6;
    padding: 24px;
}}
.container {{
    max-width: 1000px;
    margin: 0 auto;
    background: #ffffff;
    border-radius: 12px;
    border: 1px solid var(--border);
    padding: 36px;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05);
}}
.header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 2px solid var(--border);
    padding-bottom: 20px;
    margin-bottom: 24px;
}}
.badge-demo {{
    background: #38bdf8;
    color: #0f172a;
    padding: 4px 12px;
    border-radius: 9999px;
    font-weight: 700;
    font-size: 12px;
}}
.grid-metrics {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 16px;
    margin-bottom: 24px;
}}
@media (max-width: 700px) {{
    .grid-metrics {{ grid-template-columns: 1fr; }}
}}
.metric-box {{
    background: #f1f5f9;
    padding: 16px;
    border-radius: 8px;
    border-left: 4px solid var(--accent);
}}
.metric-box.danger {{ border-left-color: var(--red); }}
.metric-box.success {{ border-left-color: var(--green); }}
.metric-title {{ font-size: 12px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }}
.metric-val {{ font-size: 20px; font-weight: 800; color: var(--dark-xray); margin-top: 4px; }}
table {{
    width: 100%;
    border-collapse: collapse;
    margin: 20px 0;
    font-size: 13px;
}}
th, td {{
    padding: 12px 14px;
    text-align: left;
    border-bottom: 1px solid var(--border);
}}
th {{
    background: #f8fafc;
    font-weight: 600;
    color: var(--text-muted);
}}
.btn {{
    display: inline-block;
    background: var(--accent);
    color: #ffffff;
    padding: 12px 24px;
    border-radius: 6px;
    text-decoration: none;
    font-weight: 600;
    font-size: 14px;
}}
.btn:hover {{ background: #1d4ed8; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <h1 style="font-size: 24px; font-weight: 800; color: var(--dark-xray);">Демо-аудит: ООО «Инженерные Решения» (sample-tech.ru)</h1>
            <p style="font-size: 13px; color: var(--text-muted);">Эталонная экспресс-справка технического комплаенса по 152-ФЗ, 156-ФЗ и 38-ФЗ</p>
        </div>
        <span class="badge-demo">ДЕМОНСТРАЦИЯ</span>
    </div>

    <div class="grid-metrics">
        <div class="metric-box danger">
            <div class="metric-title">Риск КоАП (ч. 5 ст. 4.4)</div>
            <div class="metric-val">120 000 ₽</div>
            <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Ч. 2 ст. 13.11 КоАП (без умножения)</div>
        </div>
        <div class="metric-box success">
            <div class="metric-title">Горизонт стабильности (H*)</div>
            <div class="metric-val">29 недель</div>
            <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">Оптимальный интервал без пересмотра</div>
        </div>
        <div class="metric-box">
            <div class="metric-title">Статус по 248-ФЗ</div>
            <div class="metric-val" style="font-size: 16px;">ПРЕДОСТЕРЕЖЕНИЕ</div>
            <div style="font-size: 11px; color: var(--text-muted); margin-top: 4px;">10 дней на устранение без проверок</div>
        </div>
    </div>

    <h2 style="font-size: 18px; margin-bottom: 12px; color: var(--dark-xray);">1. Реестр выявленных расхождений (Traffic Light)</h2>
    <table>
        <thead>
            <tr>
                <th>Уровень</th>
                <th>Объект проверки</th>
                <th>Основание и норматив</th>
                <th>Рекомендация</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                <td><span style="color: var(--red); font-weight: 700;">[RED]</span></td>
                <td>Форма заказа (строка 142)</td>
                <td>Пакетное согласие: рассылка объединена с договором (156-ФЗ)</td>
                <td>Разделить чекбоксы на обязательный и маркетинговый</td>
            </tr>
            <tr>
                <td><span style="color: var(--amber); font-weight: 700;">[ORANGE]</span></td>
                <td>Сторонний скрипт чата</td>
                <td>Не указан оператор виджета в Политике конфиденциальности</td>
                <td>Добавить реквизиты оператора виджета в Политику</td>
            </tr>
            <tr>
                <td><span style="color: var(--green); font-weight: 700;">[GRAY]</span></td>
                <td>Базовые cookie-файлы</td>
                <td>Техническая гигиена: баннер cookie отсутствует</td>
                <td>Разместить автономный vanilla JS баннер без CDN</td>
            </tr>
        </tbody>
    </table>

    <div style="background: #0f172a; color: #ffffff; padding: 24px; border-radius: 8px; margin-top: 24px; display: flex; justify-content: space-between; align-items: center; flex-wrap: gap; gap: 16px;">
        <div>
            <div style="font-size: 18px; font-weight: 700;">Устраните все риски с помощью Комплекта AdRadar</div>
            <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">Готовые документы 2026 года, чистые сниппеты и план внедрения для программистов.</div>
        </div>
        <a href="/#order" class="btn">Заказать комплект (14 900 ₽)</a>
    </div>

    <div style="margin-top: 32px; text-align: center;">
        <a href="/" style="color: var(--accent); text-decoration: none; font-size: 14px;">← Вернуться на главную страницу</a>
    </div>
</div>
</body>
</html>
"""
