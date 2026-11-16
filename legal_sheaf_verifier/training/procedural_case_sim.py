"""
Procedural Case Simulator: Generates realistic labeled legal cases across 20 authentic
Russian advertising law practice archetypes (38-FZ, 168-FZ, KoAP Art. 4.1.1, 4.5, 14.3).
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Tuple

from legal_sheaf.schema import DocumentInput, LegalRegime, SlotID


class ProceduralCaseSimulator:
    """
    Synthesizes diverse, legally precise documents for training and testing
    LegalSheafNet-16x41 with exact ground-truth labels for:
    - is_violation (0 or 1)
    - regime (0..6)
    - slot_labels (16 binary indicators)
    """

    def __init__(self, seed: int = 42):
        self.rng = random.Random(seed)

    def _sample_inn(self, valid: bool = True) -> str:
        if valid:
            valid_inns = [
                "7707083893", "7736207543", "7702070139", "7724229333",
                "500100732259", "7706092528", "7708503727", "7801262791",
            ]
            return self.rng.choice(valid_inns)
        else:
            return "7707083894"  # Corrupted checksum

    def _sample_erid(self, valid: bool = True) -> str:
        if valid:
            chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            prefix = self.rng.choice(["2Vtzqu", "Kra23", "Pb3Xy", "4Vftx"])
            return prefix + "".join(self.rng.choice(chars) for _ in range(8))
        else:
            return "11111111"

    def generate_case(self, archetype_id: Optional[int] = None) -> Tuple[DocumentInput, Dict[str, Any]]:
        """
        Generate a document and its ground-truth annotations.
        """
        if archetype_id is None:
            archetype_id = self.rng.randint(0, 24)

        slots = [0] * 16
        meta = {}

        # Archetype 0: NATIVE_PROMO_NO_CTA
        # Subtle native promo, brand focus, positive eval, no direct word "купи", but with referral link.
        if archetype_id == 0:
            brand = self.rng.choice(["SkillHub", "DataTech", "FinMaster"])
            text = (
                f"В последнее время все чаще использую сервис {brand}. "
                f"Их платформа предлагает непревзойденное качество и самые надежные решения на рынке. "
                f"Подробнее ознакомиться можно на их официальном сайте: https://{brand.lower()}.ru/?ref=author_post."
            )
            html = f"<article><p>{text}</p></article>"
            url = f"https://blog.ru/post/{self.rng.randint(100, 999)}"
            domain = "blog.ru"
            is_violation = 1
            regime = LegalRegime.UNMARKED_AD_NO_ERID
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ATTENTION_FOCUS] = 1
            slots[SlotID.LINK_TOPOLOGY] = 1
            days_ago = self.rng.randint(5, 60)

        # Archetype 1: MULTIBRAND_EQUAL_DIGEST
        # Neutral comparative digest of 4 competing solutions, equal structure, clean non-ad.
        elif archetype_id == 1:
            text = (
                "Сравнительный аналитический обзор четырех СУБД для аналитики: "
                "ClickHouse, PostgreSQL, Apache Pinot, StarRocks. "
                "Анализируем архитектуру колоночного сжатия, скорость векторного выполнения запросов, "
                "потребление оперативной памяти и сложность администрирования кластера без выделения фаворита."
            )
            html = f"<div><h1>Обзор СУБД</h1><p>{text}</p></div>"
            url = "https://habr.com/post/12345"
            domain = "habr.com"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            slots[SlotID.MULTI_BRAND_REVIEW] = 1
            days_ago = self.rng.randint(10, 120)

        # Archetype 2: SURROGATE_PARTNER_LABEL
        # Marked with "Партнерский материал" without "Реклама".
        elif archetype_id == 2:
            brand = self.rng.choice(["EcoMarket", "FitBox", "AutoLife"])
            text = (
                f"Партнерский материал. Сеть магазинов {brand} объявляет сезонную распродажу! "
                f"Скидки до 40% на весь ассортимент по промокоду ECO2024. "
                f"Заказывайте с доставкой: https://{brand.lower()}.ru/sale"
            )
            html = f"<div class='partner'><span>Партнерский материал</span><p>{text}</p></div>"
            url = f"https://news-portal.ru/article/{self.rng.randint(1000, 9999)}"
            domain = "news-portal.ru"
            is_violation = 1
            regime = LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.CALL_TO_ACTION] = 1
            slots[SlotID.LINK_TOPOLOGY] = 1
            slots[SlotID.AD_LABEL_MARK] = 1
            days_ago = self.rng.randint(5, 50)

        # Archetype 3: LAWFUL_MARKED_AD_FULL_TRIAD
        # Complete lawful advertising triad: "Реклама", advertiser name, valid INN, valid erid.
        elif archetype_id == 3:
            inn = self._sample_inn(valid=True)
            erid = self._sample_erid(valid=True)
            company = self.rng.choice(["ООО 'ТехноМир'", "ПАО 'Сбербанк'", "ООО 'Яндекс'"])
            text = (
                f"Реклама. {company}, ИНН {inn}, erid: {erid}. "
                f"Откройте расчетный счет для бизнеса с бесплатным обслуживанием на 6 месяцев. "
                f"Подайте онлайн-заявку за 5 минут: https://bank.ru/biz?erid={erid}"
            )
            html = f"<div class='ad'><span>Реклама. {company}, ИНН {inn}, erid: {erid}</span><p>{text}</p></div>"
            url = f"https://bank.ru/biz?erid={erid}"
            domain = "biz-media.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_MARKED_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.CALL_TO_ACTION] = 1
            slots[SlotID.LINK_TOPOLOGY] = 1
            slots[SlotID.AD_LABEL_MARK] = 1
            slots[SlotID.ADVERTISER_INFO] = 1
            slots[SlotID.ERID_INTEGRITY] = 1
            days_ago = self.rng.randint(1, 40)

        # Archetype 4: UNMARKED_AFFILIATE_UTM
        # Unmarked ad with affiliate promo code and UTM link.
        elif archetype_id == 4:
            text = (
                "Крутой курс по Python-разработке! Сам прошел и всем советую. "
                "По моему личному промокоду PYPRO скидка 35% до конца недели! "
                "Регистрируйтесь по ссылке: https://code-academy.ru/?utm_source=blogger&utm_campaign=pypro"
            )
            html = f"<p>{text}</p>"
            url = "https://t.me/python_dev/123"
            domain = "t.me"
            is_violation = 1
            regime = LegalRegime.UNMARKED_AD_NO_ERID
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ATTENTION_FOCUS] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.CALL_TO_ACTION] = 1
            slots[SlotID.LINK_TOPOLOGY] = 1
            days_ago = self.rng.randint(2, 45)

        # Archetype 5: OWN_CATALOG_NEWS
        # Routine update on seller's own channel/site about catalogue.
        elif archetype_id == 5:
            text = (
                "Уважаемые клиенты! В наш фирменный каталог поступили новые модели зимней обуви. "
                "Размеры от 36 до 45 в наличии во всех магазинах сети. "
                "График работы в праздничные дни: ежедневно с 10:00 до 22:00."
            )
            html = f"<div><h2>Новости компании</h2><p>{text}</p></div>"
            url = "https://shoeshop.ru/news/winter-2024"
            domain = "shoeshop.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.SUBJECT_RELATION] = 1
            slots[SlotID.SELF_CATALOG_NORM] = 1
            days_ago = self.rng.randint(5, 80)

        # Archetype 6: OWN_SITE_PINNED_PROMO_BANNER
        # Pinned promotional banner on own site with "Buy only this product with 50% discount".
        elif archetype_id == 6:
            text = (
                "ГЛАВНАЯ АКЦИЯ ГОДА! Покупайте только наш флагманский смартфон ProMax со скидкой 50%! "
                "Только сегодня при оформлении заказа на сайте супер-подарок! Жмите сюда и покупайте прямо сейчас!"
            )
            html = f"<div class='sticky-banner-top'><h1>{text}</h1><button>КУПИТЬ СЕЙЧАС</button></div>"
            url = "https://electronics.ru"
            domain = "electronics.ru"
            is_violation = 1
            regime = LegalRegime.UNMARKED_AD_NO_ERID
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ATTENTION_FOCUS] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.CALL_TO_ACTION] = 1
            days_ago = self.rng.randint(1, 20)

        # Archetype 7: FOREIGN_LANG_168FZ_SALE
        # English promotional words without Russian translation (168-FZ).
        elif archetype_id == 7:
            text = (
                "GRAND MEGA SALE! BLACK FRIDAY DISCOUNTS UP TO 70%! "
                "Special offer: buy 2 get 1 FREE! Best price in fashion retail! "
                "Заказывайте одежду в нашем интернет-магазине с быстрой доставкой."
            )
            html = f"<div class='hero'><h1>MEGA SALE</h1><p>{text}</p></div>"
            url = "https://fashion-outlet.ru/sale"
            domain = "fashion-outlet.ru"
            is_violation = 1
            regime = LegalRegime.FOREIGN_LANG_168FZ
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.RUSSIAN_LANG_168FZ] = 1
            days_ago = self.rng.randint(3, 30)

        # Archetype 8: SPECIAL_REGIME_MEDICINE_NO_DISCLAIMER
        # Medical / cosmetic procedures without mandatory disclaimer.
        elif archetype_id == 8:
            text = (
                "Клиника эстетической медицины 'Грация'. Процедуры лазерного омоложения и контурной пластики. "
                "Видимый результат уже после первого сеанса. Запись на прием по телефону 8-800-555-35-35."
            )
            html = f"<div><p>{text}</p></div>"
            url = "https://clinic-gracia.ru"
            domain = "clinic-gracia.ru"
            is_violation = 1
            regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.SPECIAL_BANNED] = 1
            days_ago = self.rng.randint(10, 60)

        # Archetype 9: SPECIAL_REGIME_FINANCIAL_CREDIT_RATE
        # Financial credit card ad with 0% rate without full terms.
        elif archetype_id == 9:
            text = (
                "Кредит наличными под 0% годовых! Получите до 1 000 000 рублей прямо сейчас без отказа и справок! "
                "Оформляйте онлайн в Банке 'Быстрые Деньги'!"
            )
            html = f"<div><p>{text}</p></div>"
            url = "https://fast-bank.ru"
            domain = "fast-bank.ru"
            is_violation = 1
            regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.SPECIAL_BANNED] = 1
            days_ago = self.rng.randint(5, 40)

        # Archetype 10: BANNED_VPN_OBVIOUS
        # Banned VPN service advertisement.
        elif archetype_id == 10:
            text = (
                "Лучший VPN-сервис для обхода любых блокировок сайтов и соцсетей! "
                "Быстрый доступ к запрещенным ресурсам на максимальной скорости. Скачивайте приложение по ссылке!"
            )
            html = f"<div><p>{text}</p></div>"
            url = "https://vpn-free.net"
            domain = "channel.ru"
            is_violation = 1
            regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.SPECIAL_BANNED] = 1
            days_ago = self.rng.randint(2, 25)

        # Archetype 11: OVERMARKED_SCIENTIFIC_CASE
        # Overmarked scientific or educational article with accidental erid.
        elif archetype_id == 11:
            erid = self._sample_erid(valid=True)
            text = (
                f"Реклама. erid: {erid}. Исследование микроархитектуры нейронных сетей и топологических пучков. "
                "В статье проведен математический анализ клеточных когомологий на графах ограничений. "
                "Материал носит исключительно образовательный характер."
            )
            html = f"<div><span>Реклама. erid: {erid}</span><p>{text}</p></div>"
            url = "https://ai-research.org/paper/77"
            domain = "ai-research.org"
            is_violation = 0
            regime = LegalRegime.OVER_MARKED_NON_AD
            slots[SlotID.AD_LABEL_MARK] = 1
            slots[SlotID.ERID_INTEGRITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            days_ago = self.rng.randint(15, 90)

        # Archetype 12: STALE_POST_OVER_1YEAR
        # Post older than 365 days (KoAP Art. 4.5 statute of limitations).
        elif archetype_id == 12:
            text = (
                "Старый пост со скидкой на курс 2022 года. Записывайтесь со скидкой 20% по промокоду OLD2022."
            )
            html = f"<p>{text}</p>"
            url = "https://oldblog.ru/post/55"
            domain = "oldblog.ru"
            is_violation = 0  # Procedural defense: limitation expired!
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.PROCEDURAL_KOAP] = 1
            days_ago = self.rng.randint(380, 500)

        # Archetype 13: SME_FIRST_TIME_INFRACTION
        # Small business first-time post eligible for warning under Art. 4.1.1 KoAP.
        elif archetype_id == 13:
            text = (
                "Микропредприятие 'Уютная Пекарня' приглашает за свежим хлебом и домашней выпечкой! "
                "Приходите в нашу кондитерскую на углу улицы Ленина, скидка 10% на утренний кофе!"
            )
            html = f"<p>{text}</p>"
            url = "https://vk.com/bakery_local"
            domain = "vk.com"
            is_violation = 1
            regime = LegalRegime.UNMARKED_AD_NO_ERID
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.PROCEDURAL_KOAP] = 1
            days_ago = self.rng.randint(5, 30)

        # Archetype 14: ERID_IN_JSON_METADATA_ONLY
        # Token in metadata, label and INN in text -> Lawful Clean Marked Ad!
        elif archetype_id == 14:
            inn = self._sample_inn(valid=True)
            erid = self._sample_erid(valid=True)
            text = (
                f"Реклама. ООО 'Цифровые Системы', ИНН {inn}. "
                "Облачные серверы и хранилища данных для enterprise-клиентов. "
                "Подробнее на сайте https://cloud-systems.ru"
            )
            html = f"<div class='ad-block'><p>{text}</p></div>"
            url = "https://tech-media.ru/article/88"
            domain = "tech-media.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_MARKED_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.AD_LABEL_MARK] = 1
            slots[SlotID.ADVERTISER_INFO] = 1
            slots[SlotID.ERID_INTEGRITY] = 1
            days_ago = self.rng.randint(1, 20)
            meta = {"ad_data": {"erid": erid, "token": erid}}
            doc = DocumentInput(
                clean_text=text,
                raw_html=html,
                page_url=url,
                author_domain=domain,
                platform_ad_meta=meta,
                published_days_ago=days_ago,
            )
            target = {
                "is_violation": is_violation,
                "regime": regime.value,
                "slot_labels": slots,
            }
            return doc, target

        # Archetype 15: MISSING_INN_ONLY
        # Has "Реклама" and erid, but missing advertiser name/INN!
        elif archetype_id == 15:
            erid = self._sample_erid(valid=True)
            text = (
                f"Реклама. erid: {erid}. "
                "Заказывайте доставку пиццы и бургеров со скидкой 20% в приложении!"
            )
            html = f"<div><span>Реклама. erid: {erid}</span><p>{text}</p></div>"
            url = "https://food-delivery.ru"
            domain = "food-delivery.ru"
            is_violation = 1
            regime = LegalRegime.INCOMPLETE_MARKING_LABEL_OR_INN
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.AD_LABEL_MARK] = 1
            slots[SlotID.ERID_INTEGRITY] = 1
            days_ago = self.rng.randint(2, 35)

        # Archetype 16: ORGANIC_LIFESTYLE_STORY
        # Natural mention in author's personal lifestyle blog without ad purpose.
        elif archetype_id == 16:
            text = (
                "Во время путешествия по Алтаю останавливались на базе отдыха 'Кедровая Поляна'. "
                "Очень понравилась баня с купелью и вид на заснеженные вершины. "
                "Делюсь фотографиями природы и красивыми закатами."
            )
            html = f"<article><p>{text}</p></article>"
            url = "https://travel-blog.ru/post/altai"
            domain = "travel-blog.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ORGANIC_INTEGRATION] = 1
            days_ago = self.rng.randint(10, 80)

        # Archetype 17: PRESS_RELEASE_NEWS_MEDIA
        # Routine press release in business news.
        elif archetype_id == 17:
            text = (
                "Пресс-релиз: Совет директоров металлургического комбината утвердил годовой отчет "
                "и программу экологической модернизации производства на 2024–2026 годы. "
                "Выручка предприятия за отчетный период выросла на 8%."
            )
            html = f"<div class='news-item'><p>{text}</p></div>"
            url = "https://rbc.ru/business/release"
            domain = "rbc.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            days_ago = self.rng.randint(5, 50)

        # Archetype 18: LOTTERY_NO_RULES_ART9
        # Instagram/Telegram giveaway of gadget without rules, dates, organizer.
        elif archetype_id == 18:
            text = (
                "ВНИМАНИЕ РОЗЫГРЫШ! Дарю новенький iPhone 15 Pro Max среди подписчиков канала! "
                "Просто подпишись, поставь лайк и отметь друзей! Победителя определю через генератор случайных чисел!"
            )
            html = f"<p>{text}</p>"
            url = "https://t.me/blogger_giveaway/99"
            domain = "t.me"
            is_violation = 1
            regime = LegalRegime.BANNED_OR_SPECIAL_REGIME
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ATTENTION_FOCUS] = 1
            slots[SlotID.SPECIAL_BANNED] = 1
            days_ago = self.rng.randint(1, 15)

        # Archetype 19: CONCEALED_SINGLE_BRAND_BIAS
        # Disguised review favoring one brand with discount code & link.
        elif archetype_id == 19:
            text = (
                "Сравнение пяти сервисов доставки: у четырех курьеры опаздывают и еда остывает, "
                "а вот сервис 'ЭкспрессГурмэ' работает идеально! Быстрая доставка за 25 минут и горячие блюда. "
                "По моему промокоду GOURMET скидка 300 руб. на первый заказ: https://express-gourmet.ru/order"
            )
            html = f"<div class='review'><p>{text}</p></div>"
            url = "https://foodie-blog.ru/post/5"
            domain = "foodie-blog.ru"
            is_violation = 1
            regime = LegalRegime.UNMARKED_AD_NO_ERID
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ATTENTION_FOCUS] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            slots[SlotID.CALL_TO_ACTION] = 1
            slots[SlotID.LINK_TOPOLOGY] = 1
            days_ago = self.rng.randint(3, 40)

        # Archetype 20: CLINIC_OWN_WEBSITE_MANDATORY_INFORMING (Hard Negative 1)
        # Listing services, doctors, qualifications, prices on clinic's own site (Art. 79 323-FZ, Decree 736)
        elif archetype_id == 20:
            text = (
                "Медицинский центр «Здоровье». Перечень практикующих врачей, стаж, ученые степени и квалификационные категории. "
                "Прейскурант платных медицинских услуг: консультация терапевта 2500 руб., прием кардиолога высшей категории 3500 руб. "
                "Информация размещена в порядке обязательного информирования потребителей по ст. 79 Федерального закона № 323-ФЗ и ПП РФ № 736."
            )
            html = f"<div class='clinic-doctors'><p>{text}</p></div>"
            url = "https://clinic-zdorovie.ru/doctors"
            domain = "clinic-zdorovie.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            slots[SlotID.COMMERCIAL_TERMS] = 1
            meta = {"context_mode": "OWN_WEBSITE"}
            days_ago = self.rng.randint(5, 50)

        # Archetype 21: HR_CAREER_VACANCIES (Hard Negative 2)
        # Corporate open vacancies page with salary in rubles and apply button (Art. 2 38-FZ)
        elif archetype_id == 21:
            text = (
                "Карьера в ИТ-компании «ДатаСистемс». Открыта вакансия Senior Backend Developer. "
                "Заработная плата от 350 000 руб. на руки, официальное оформление, ДМС со стоматологией, компенсация фитнеса. "
                "Стек: Python, FastAPI, PostgreSQL, Docker, Kubernetes. Нажмите кнопку «Откликнуться» для отправки резюме."
            )
            html = f"<div class='vacancies'><p>{text}</p><button>Откликнуться</button></div>"
            url = "https://datasystems.ru/careers"
            domain = "datasystems.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            meta = {"context_mode": "OWN_WEBSITE"}
            days_ago = self.rng.randint(3, 40)

        # Archetype 22: B2B_INDUSTRIAL_PLANT_ISO_OEM (Hard Negative 3)
        # B2B industrial plant technical catalog with ISO standards, API, SLA, and OEM terms
        elif archetype_id == 22:
            text = (
                "Завод промышленной арматуры и гидравлики. Поставки регулирующих клапанов и насосных агрегатов для предприятий нефтегазового комплекса. "
                "Система менеджмента качества сертифицирована по международному стандарту ISO 9001:2015. "
                "B2B-поставки, контрактное OEM-производство, мониторинг остатков через REST API и гарантия доступности SLA 99.9%."
            )
            html = f"<div class='plant-specs'><p>{text}</p></div>"
            url = "https://prom-armatura.ru/tech-catalog"
            domain = "prom-armatura.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            meta = {"context_mode": "OWN_WEBSITE"}
            days_ago = self.rng.randint(10, 90)

        # Archetype 23: RAS_DICTIONARY_WORDS_AND_TRADEMARK (Hard Negative 4)
        # Page with approved RAS loanwords (кешбэк, маркетинг) and registered trademark BRAND®
        elif archetype_id == 23:
            text = (
                "Программа лояльности для покупателей бренда NOVALUX®. "
                "Получайте повышенный кешбэк до 10% бонусными рублями за каждый онлайн-заказ через наш официальный сайт. "
                "Персональный менеджер, бесплатная доставка в наш фирменный шоурум и прозрачный тариф на сервисное обслуживание."
            )
            html = f"<div class='loyalty-info'><p>{text}</p></div>"
            url = "https://novalux.ru/loyalty"
            domain = "novalux.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            meta = {"context_mode": "OWN_WEBSITE"}
            days_ago = self.rng.randint(5, 60)

        # Archetype 24: UNIFORM_DEALER_PARTNER_LIST (Hard Negative 5)
        # Uniform dealer list ("Where to buy") across 5 partner companies without highlighting
        else:
            text = (
                "Где купить нашу продукцию? Единообразный перечень официальных авторизованных дилеров и региональных партнеров: "
                "1. ООО «СеверАвто» — г. Санкт-Петербург, Московский пр-т, 12; "
                "2. ТД «УралЗапчасть» — г. Екатеринбург, ул. Малышева, 45; "
                "3. Компания «СибирьТрейд» — г. Новосибирск, Красный пр-т, 80; "
                "4. «ЮгМоторс» — г. Ростов-на-Дону, пр-т Ворошиловский, 22; "
                "5. «ПоволжьеСнаб» — г. Казань, ул. Спартаковская, 14. "
                "Все авторизованные дистрибьюторы работают по единым рекомендованным розничным ценам производителя."
            )
            html = f"<div class='dealers-where-to-buy'><p>{text}</p></div>"
            url = "https://autocomponent.ru/where-to-buy"
            domain = "autocomponent.ru"
            is_violation = 0
            regime = LegalRegime.CLEAN_NON_AD
            slots[SlotID.OBJECT_IDENTITY] = 1
            slots[SlotID.ANALYTICAL_SHIELD] = 1
            slots[SlotID.MULTI_BRAND_REVIEW] = 1
            meta = {"context_mode": "OWN_WEBSITE"}
            days_ago = self.rng.randint(15, 100)

        doc = DocumentInput(
            clean_text=text,
            raw_html=html,
            page_url=url,
            author_domain=domain,
            platform_ad_meta=meta,
            published_days_ago=days_ago,
        )
        target = {
            "is_violation": is_violation,
            "regime": regime.value,
            "slot_labels": slots,
        }
        return doc, target

    def generate_dataset(self, num_samples: int = 500) -> List[Tuple[DocumentInput, Dict[str, Any]]]:
        """Generate a balanced dataset covering all 25 archetypes."""
        dataset = []
        for i in range(num_samples):
            arch_id = i % 25
            dataset.append(self.generate_case(arch_id))
        self.rng.shuffle(dataset)
        return dataset
