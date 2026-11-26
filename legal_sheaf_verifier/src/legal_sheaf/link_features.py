"""
LinkCentricFeatureExtractor: Builds an honest, non-redundant 48-dim feature vector
for LinkCentricAdClassifierV3, anchored on commercial link context windows (+/-350 chars)
rather than full-article dilution or synthetic precedent-slot duplication.
"""

from __future__ import annotations

import math
import os
import re
import urllib.parse
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import torch

from legal_sheaf.config import MODELS_CACHE_DIR
from legal_sheaf.inn_erid_validators import (
    COMMERCIAL_URL_PARAM_REGEX,
    SHORTENER_HOSTS,
    analyze_ad_label_status,
    detect_untranslated_foreign_words_168fz,
    extract_commercial_targets,
    extract_erid_tokens,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    has_custom_campaign_or_service_path,
    is_editorial_crypto_news_url,
)
from legal_sheaf.schema import DocumentInput

FEATURE_DIM = 48
FEATURE_SCHEMA_VERSION = "link_centric_v3.0"

FEATURE_NAMES: Tuple[str, ...] = (
    # 0..11: Link & Channel Topology
    "has_any_commercial_target",
    "log_unique_comm_domains",
    "has_website_url",
    "has_messenger_bot_or_max",
    "has_affiliate_or_utm_or_shortener",
    "has_custom_vc_campaign_or_service_path",
    "is_single_neutral_tg_footer",
    "link_tail_position_ratio",
    "has_bare_domain_or_at_handle",
    "has_phone_contact",
    "unique_brands_ratio",
    "top_brand_dominance",
    # 12..21: Link-Window Commercial Signals
    "win_has_promo_code_or_giveaway",
    "win_has_price_or_discount",
    "win_has_cta_imperative",
    "win_has_self_product_pitch",
    "win_has_superlative",
    "doc_commercial_density",
    "has_strict_ad_label",
    "has_surrogate_ad_label",
    "has_valid_inn_or_ogrn",
    "has_valid_erid",
    # 22..27: Statutory Context, Exemptions & Special Regimes
    "is_own_website",
    "is_uniform_multibrand_shield",
    "has_editorial_news_shield",
    "has_168fz_foreign_violation",
    "has_banned_or_special_trigger",
    "is_statute_expired_365d",
    # 28..37: Lead-Window Semantic Cosine vs 10 Prototypes
    "lead_proto_0_direct_promo",
    "lead_proto_1_native_saas",
    "lead_proto_2_tg_bot_aff",
    "lead_proto_3_b2b_services",
    "lead_proto_4_banned_special",
    "lead_proto_5_editorial_news",
    "lead_proto_6_author_blog",
    "lead_proto_7_multi_review",
    "lead_proto_8_tech_docs",
    "lead_proto_9_own_catalog_hr",
    # 38..47: Link-Window Semantic Cosine vs 10 Prototypes
    "link_proto_0_direct_promo",
    "link_proto_1_native_saas",
    "link_proto_2_tg_bot_aff",
    "link_proto_3_b2b_services",
    "link_proto_4_banned_special",
    "link_proto_5_editorial_news",
    "link_proto_6_author_blog",
    "link_proto_7_multi_review",
    "link_proto_8_tech_docs",
    "link_proto_9_own_catalog_hr",
)

assert len(FEATURE_NAMES) == FEATURE_DIM

SEMANTIC_LEGAL_PROTOTYPES_V3: Tuple[str, ...] = (
    # 0: Direct promo/discount/promocode/CTA sale
    "Купите товар или закажите услугу со скидкой по промокоду, выгодный тариф и цены по ссылке на сайте компании",
    # 1: Native B2B SaaS/startup product pitch
    "Мы разработали и запустили удобный коммерческий сервис для бизнеса, попробуйте продукт в демо-режиме, команда и сайт сервиса",
    # 2: Telegram bot / paid channel / affiliate recommendation
    "Запустили телеграм-бот и партнерский сервис с подпиской и бонусом по реферальной ссылке в канале",
    # 3: Agency / B2B soft case study linking to /services/ or product page
    "Корпоративный кейс разработки и продуктовой поддержки для клиента, подробнее об услугах агентства и консультациях на сайте",
    # 4: Banned / special regime promotion
    "Рабочий VPN для обхода блокировок, сравнение криптообменников для обмена USDT и биткоина, бонус криптобиржи по партнерской ссылке, онлайн-казино, кредит 0 процентов, клиника без противопоказаний",
    # 5: Neutral editorial news / security incident / macro statistics
    "Редакционная новость о взломе и возмещении активов, деловое издание, макростатистика рынка и курс валют без продажи услуг",
    # 6: Personal author blog / lifestyle story with neutral channel footer
    "Личный опыт, авторские заметки о путешествиях, карьере и менеджменте, короткий блог веду в своем авторском телеграм-канале",
    # 7: Objective uniform multi-brand comparative benchmark
    "Сравнительный аналитический обзор десяти конкурирующих моделей и сервисов в едином стиле без выделения одного продавца и без партнерской продажи",
    # 8: Technical developer tutorial / GitHub / official docs & help-center
    "Техническая документация разработчика, официальная справка Help Center, репозиторий GitHub и пошаговая инструкция по настройке кода и API",
    # 9: Own company catalog / mandatory clinic informing (323-FZ) / HR / B2B ISO
    "Официальный каталог товаров и прайс-лист клиники по 323-ФЗ, открытые вакансии и карьера в компании, промышленные стандарты ISO, SLA и OEM",
)

_UGC_PLATFORM_HOSTS: Set[str] = {
    "vc.ru", "habr.com", "dzen.ru", "t.me", "telegram.me",
    "vk.com", "pikabu.ru", "tenchat.ru", "youtube.com", "max.ru", "ok.ru",
}

_NON_BRAND_UPPER_WORDS: Set[str] = {
    "HTTP", "HTTPS", "HTML", "JSON", "REST", "API", "SDK", "VPN", "URL", "CLI",
    "USB", "ISO", "SLA", "OEM", "SQL", "CSS", "PNG", "JPG", "PDF", "FAQ", "B2B",
    "B2C", "USDT", "BEP20", "ERC20", "TRON", "CEO", "GPT", "LLM", "SaaS",
}

_SHARED_USER2_ENCODERS: Dict[Tuple[str, str], Any] = {}

_LINKS_TAIL_STRIP_REGEX = re.compile(r"\n\[Ссылки:[^\]]*\]\s*$")
_PROMO_CODE_OR_GIVEAWAY_REGEX = re.compile(
    r"(?i)\b(?:промокод[а-я]*\s*[:\s\-«\"]*[a-zA-Z0-9_-]{3,24}|скидк\w*\s+по\s+(?:моему\s+)?промокоду|розыгрыш\s+призов)\b"
)
_GIVEAWAY_WORD_REGEX = re.compile(r"(?i)\b(?:розыгрыш|разыгрываем|дарим\s+приз|дарю\s+\w+)\b")
_GIVEAWAY_SUB_REGEX = re.compile(r"(?i)\b(?:подпиш|подписк|отметь\s+(?:друз|трех)|лайк)\b")

_PRICE_DISCOUNT_REGEX = re.compile(
    r"(?i)\b(?:цена|стоимость|тариф\w*|\d+\s*(?:руб(?:лей|\.)?|₽)|скидк\w*|\d+%\s*(?:скидк|кешбэк|кэшбэк)|"
    r"акция|распродаж\w*|спеццена|демо[-\s]*режим\w*|бесплатн\w+\s+(?:доступ|триал|период|тариф|подписк|обслуживани))\b"
)
_CTA_IMPERATIVE_REGEX = re.compile(
    r"(?i)\b(?:купи(?:те)?|покупай(?:те)?|заказывай(?:те)?|закажи(?:те)?|переходи(?:те)?|"
    r"регистрируй(?:тесь)?|регай(?:тесь)?|подписывай(?:тесь)?|оформляй(?:те)?|попробуй(?:те)?|"
    r"попробовать\s+\w+|подключи(?:те)?|скачай(?:те)?|скачивай(?:те)?|забирай(?:те)?|жми(?:те)?|"
    r"подайте\s+онлайн-заявку|записывайтесь|забронируй(?:те)?|пишите\s+в\s+(?:telegram|тг|лс)|заходите\s+на)\b"
)
_SELF_PRODUCT_PITCH_REGEX = re.compile(
    r"(?i)(?:\bнаш(?:его|ей|их)?\s+(?:сервис\w*|продукт\w*|сайт\w*|бот\w*|инструмент\w*|блог\w*|курс\w*|решени\w*)|"
    r"\bмы\s+(?:разработали|создали|запустили|сделали|протестировали|подготовили|развиваем)|"
    r"\bя\s+(?:развиваю|создал|запустил|руководитель|основатель|ceo)\b|"
    r"\bкоманд[аеуой]+\s+[А-ЯA-ZЁ][а-яёa-zA-Z0-9\-]{2,}|"
    r"\bceo\s+[А-ЯA-ZЁ][а-яёa-zA-Z0-9\-]{2,}|"
    r"\bсайт\s*:\s*[a-z0-9\-]+\.[a-z]{2,})"
)
_SUPERLATIVE_REGEX = re.compile(
    r"(?i)\b(?:лучш(?:ий|ая|ее|ие|их)|№\s*1|номер\s+один|топов(?:ый|ая|ое|ые)|"
    r"уникальн(?:ый|ая|ое)|идеальн(?:ый|ая|ое|о)|непревзойденн(?:ый|ое)|главн(?:ая|ый)\s+акци\w+|самый\s+\w+)\b"
)
_EDITORIAL_NEWS_SHIELD_REGEX = re.compile(
    r"(?i)(?:#новости\b|\bпресс-релиз\b|\bне\s+(?:является|заменяет)\s+(?:индивидуальн\w+\s+)?инвестиционн\w+\s+рекомендац|"
    r"\bинформационно-аналитический\s+характер|\bвзлом\w*|\bкибератак\w*|\bвозместил[аи]?\s+клиентам|"
    r"\bгодовой\s+отчет|\bобзор\s+рынка\b|\bмакроэкономическ\w+)"
)

_CRYPTO_TERMS_REGEX = re.compile(
    r"(?i)\b(?:биткоин\w*|bitcoin|btc|ethereum|eth|usdt|криптовалют\w*|криптобирж\w*|криптообменник\w*|okx|binance|bybit|bingx|bestchange)\b"
)
_CRYPTO_EXCHANGER_REGEX = re.compile(
    r"(?i)\b(?:криптообменник\w*|обмен\w*\s+(?:usdt|криптовалют\w*|крипт\w*|биткоин\w*)|выбор\s+для\s+(?:небольшой\s+)?продажи\s+usdt)\b"
)
_CRYPTO_EXCHANGER_COMMERCIAL_REGEX = re.compile(
    r"(?i)(?:партн[её]рск\w*\s+ссылк|вознаграждени\w*|комисси\w*|обмен\w*\s+usdt|от\s+\d+\s*usdt|получение\s+сом|на\s+карт)"
)
_CRYPTO_NEWS_INCIDENT_REGEX = re.compile(
    r"(?i)(?:#новости\b|\bвзлом\w*|\bвозместил[аи]?|\bкибератак\w*|\bукраденн\w*|\bутечк\w*|\bколебал\w*\s+в\s+диапазоне|\bкурс\s+\w+\s+на\s+спотов)"
)
_CRYPTO_AFFILIATE_URL_REGEX = re.compile(
    r"(?i)(?:[?&](?:ref|aff|affiliate|partner|invite|promo)=|/(?:register|signup|exchange|swap)(?:/|$|\?)|bestchange|partner\.com)"
)
_CRYPTO_COMMERCIAL_CTA_REGEX = re.compile(
    r"(?i)\b(?:купи(?:те)?|покупай(?:те)?|регистрируй(?:тесь)?|регай(?:тесь)?|открой(?:те)?\s+(?:счет|кошелек)|обменяй(?:те)?|промокод)\b"
)

_NON_CRYPTO_VPN_OR_GAMBLING_REGEX = re.compile(
    r"(?i)\b(?:vpn[-\s]*(?:сервис\w*|бот\w*|приложени\w*|подписк\w*|клиент\w*)|скача(?:ть|йте)\s+vpn|лучш(?:ий|ее)\s+vpn|обход\s+блокировок|"
    r"онлайн[-\s]*казино|казино|онлайн[-\s]*ставки|ставки\s+на\s+спорт|делать\s+ставки|букмекер|фриспин\w*)\b"
)
_NON_CRYPTO_SPECIAL_PROMO_REGEX = re.compile(
    r"(?i)\b(?:кредит\s+(?:наличными\s+)?под\s+0%|доходность\s+\d+%|розыгрыш\s+призов|"
    r"лазерн(?:ое|ого)\s+омоложени|контурн(?:ой|ая)\s+пластик|эстетическ(?:ой|ая)\s+медицин)\b"
)


def get_shared_user2_encoder(device_str: str):
    """Loads and caches deepvk/USER2-base SentenceTransformer once per (model, device)."""
    key = ("deepvk/USER2-base", device_str)
    if key in _SHARED_USER2_ENCODERS:
        return _SHARED_USER2_ENCODERS[key]

    os.environ.setdefault("HF_HOME", str(MODELS_CACHE_DIR))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(MODELS_CACHE_DIR))
    from sentence_transformers import SentenceTransformer

    enc = SentenceTransformer("deepvk/USER2-base", device=device_str)
    if device_str.startswith("cuda"):
        enc.half()
    _SHARED_USER2_ENCODERS[key] = enc
    return enc


def compute_ngram_hash_vector(text: str, dim: int = 256) -> np.ndarray:
    """Deterministic subword n-gram hash fallback (< 0.05 ms) via zlib.crc32."""
    vec = np.zeros(dim, dtype=np.float32)
    text_lower = (text or "").lower()
    words = re.findall(r"\w+", text_lower)

    for w in words:
        b = w.encode("utf-8")
        h = zlib.crc32(b) % dim
        sign = 1.0 if ((zlib.crc32(b + b"_s") & 1) == 0) else -1.0
        vec[h] += sign * (1.0 + math.log1p(len(w)))

    for n in (3, 4):
        for i in range(max(0, len(text_lower) - n + 1)):
            gb = text_lower[i : i + n].encode("utf-8")
            h = zlib.crc32(gb) % dim
            sign = 1.0 if ((zlib.crc32(gb + b"_s") & 1) == 0) else -1.0
            vec[h] += sign * 0.5

    norm = np.linalg.norm(vec)
    if norm > 1e-7:
        vec /= norm
    return vec


def resolve_is_own_website(doc: DocumentInput) -> bool:
    """Resolves OWN_WEBSITE vs EXTERNAL_PUBLICATION without granting UGC platforms own-site immunity."""
    mode = getattr(doc, "context_mode", "AUTO")
    if mode == "AUTO" and isinstance(doc.platform_ad_meta, dict) and doc.platform_ad_meta.get("context_mode"):
        mode = str(doc.platform_ad_meta.get("context_mode"))
    if mode == "OWN_WEBSITE":
        return True
    if mode == "EXTERNAL_PUBLICATION":
        return False

    a_net = (doc.author_domain or "").lower().strip().split(":")[0].removeprefix("www.")
    p_net = urllib.parse.urlparse(doc.page_url).netloc.lower().split(":")[0].removeprefix("www.") if doc.page_url else ""
    if a_net and a_net not in _UGC_PLATFORM_HOSTS and p_net not in _UGC_PLATFORM_HOSTS:
        if p_net:
            return (a_net in p_net) or (p_net in a_net)
        return True
    return False


def strip_links_footer(text: str) -> str:
    """Strips synthetic '\\n[Ссылки: ...]' tail appended by crawler to recover body prose."""
    return _LINKS_TAIL_STRIP_REGEX.sub("", text or "").strip()


def _extract_target_search_keys(target: str) -> List[str]:
    keys: List[str] = []
    t = (target or "").strip()
    if not t:
        return keys
    if t.startswith(("http://", "https://")):
        keys.append(t)
        try:
            parsed = urllib.parse.urlparse(t)
            host = parsed.netloc.lower().split(":")[0].removeprefix("www.")
            if host in ("t.me", "telegram.me", "max.ru", "vk.com"):
                slug = (parsed.path or "").strip("/").split("/")[0]
                if len(slug) >= 4:
                    keys.append(slug)
            elif host:
                keys.append(host)
                stem = host.split(".")[0]
                if len(stem) >= 4 and stem not in ("mail", "blog", "news", "info", "shop"):
                    keys.append(stem)
        except Exception:
            pass
    else:
        keys.append(t)
        if "." in t and not t.startswith("@"):
            stem = t.split(".")[0]
            if len(stem) >= 4:
                keys.append(stem)
        elif t.startswith("@") and len(t) >= 5:
            keys.append(t[1:])
    return list(dict.fromkeys(k for k in keys if len(k) >= 4))


def extract_link_context_windows(
    text: str,
    targets: Dict[str, Any],
    window_radius: int = 350,
) -> Tuple[str, str, List[str], float]:
    """
    Extracts focused context windows around commercial links/anchors plus article lead & tail.
    Returns: (lead_window, combined_link_window, individual_windows[:3], mean_rel_position)
    """
    body_text = strip_links_footer(text)
    body_len = max(1, len(body_text))
    lead_window = body_text[:450]
    tail_window = body_text[-450:] if len(body_text) > 450 else body_text

    all_targets = list(targets.get("all_targets") or [])
    body_lower = body_text.lower()
    anchor_spans: List[Tuple[int, int]] = []
    rel_positions: List[float] = []

    for tgt in all_targets[:12]:
        matched_positions: List[int] = []
        for key in _extract_target_search_keys(tgt):
            idx = body_lower.find(key.lower())
            if idx != -1:
                matched_positions.append(idx)
                break
        if matched_positions:
            for pos in matched_positions[:2]:
                st = max(0, pos - window_radius)
                en = min(len(body_text), pos + window_radius)
                anchor_spans.append((st, en))
                rel_positions.append(pos / float(body_len))
        else:
            # Link appended only in [Ссылки: ...] footer from <a href>
            rel_positions.append(1.0)

    # Merge overlapping spans
    anchor_spans.sort()
    merged: List[Tuple[int, int]] = []
    for st, en in anchor_spans:
        if merged and st <= merged[-1][1] + 60:
            merged[-1] = (merged[-1][0], max(merged[-1][1], en))
        else:
            merged.append((st, en))

    windows: List[str] = []
    for st, en in merged[:3]:
        snip = body_text[st:en].strip()
        if snip and snip not in windows:
            windows.append(snip)

    if tail_window and not any(tail_window[-120:] in w for w in windows):
        windows.append(tail_window)
    elif not windows and tail_window:
        windows.append(tail_window)

    # Combine windows up to 1000 chars, always preserving tail context
    if len(windows) == 1:
        combined = windows[0][:1000]
    else:
        tail_part = windows[-1][:480]
        anchor_parts: List[str] = []
        rem = 1000 - len(tail_part) - 4
        for w in windows[:-1]:
            if rem <= 80:
                break
            take = w[:rem].strip()
            if take:
                anchor_parts.append(take)
                rem -= len(take) + 3
        combined = " ... ".join(anchor_parts + [tail_part]) if anchor_parts else tail_part

    mean_pos = float(np.mean(rel_positions)) if rel_positions else 0.0
    return lead_window, combined[:1000], windows[:3], float(np.clip(mean_pos, 0.0, 1.0))


def detect_banned_or_special_context(
    text: str,
    extracted_targets: Dict[str, Any],
    is_own_website: bool,
) -> Dict[str, Any]:
    """
    Detects genuine Part 11 Art. 7 38-FZ crypto promotion (including crypto-exchanger catalogs),
    VPN bypass promos, gambling, and special regimes WITHOUT false positives on crypto news incidents.
    """
    body_text = strip_links_footer(text)
    all_urls = list(extracted_targets.get("commercial_urls") or []) + list(extracted_targets.get("messenger_links") or [])
    website_urls = list(extracted_targets.get("website_urls") or [])
    has_targets = bool(extracted_targets.get("all_targets"))

    has_crypto_terms = bool(_CRYPTO_TERMS_REGEX.search(body_text))
    non_news_urls = [u for u in all_urls if not is_editorial_crypto_news_url(u)]
    has_crypto_aff_url = any(_CRYPTO_AFFILIATE_URL_REGEX.search(u) for u in non_news_urls)
    has_crypto_cta = bool(_CRYPTO_COMMERCIAL_CTA_REGEX.search(body_text))
    has_crypto_news_incident = bool(_CRYPTO_NEWS_INCIDENT_REGEX.search(body_text)) and not has_crypto_cta and not has_crypto_aff_url

    has_exchanger_catalog_promo = bool(
        _CRYPTO_EXCHANGER_REGEX.search(body_text)
        and len(website_urls) >= 1
        and _CRYPTO_EXCHANGER_COMMERCIAL_REGEX.search(body_text)
        and not has_crypto_news_incident
    )

    has_crypto_banned = False
    if has_crypto_terms and not has_crypto_news_incident:
        if (non_news_urls or is_own_website) and (has_crypto_aff_url or has_crypto_cta):
            has_crypto_banned = True
        elif has_exchanger_catalog_promo:
            has_crypto_banned = True

    has_comm_context = bool(
        is_own_website
        or has_targets
        or _PROMO_CODE_OR_GIVEAWAY_REGEX.search(body_text)
        or (_GIVEAWAY_WORD_REGEX.search(body_text) and _GIVEAWAY_SUB_REGEX.search(body_text))
    )
    has_vpn_or_gambling = bool(_NON_CRYPTO_VPN_OR_GAMBLING_REGEX.search(body_text)) and has_comm_context

    has_special_offer = False
    if has_comm_context:
        if _NON_CRYPTO_SPECIAL_PROMO_REGEX.search(body_text):
            # Exempt if mandatory statutory disclaimers are already present
            has_med_disclaimer = bool(re.search(r"(?i)(?:противопоказан|консультация\s+специалиста)", body_text))
            has_fin_terms = bool(re.search(r"(?i)(?:полная\s+стоимость|пск|условия\s+предоставления)", body_text))
            if re.search(r"(?i)(?:лазерн|пластик|медицин)", body_text) and not has_med_disclaimer:
                has_special_offer = True
            elif re.search(r"(?i)(?:кредит\s+(?:наличными\s+)?под\s+0%|доходность\s+\d+%)", body_text) and not has_fin_terms:
                has_special_offer = True
            elif re.search(r"(?i)розыгрыш\s+призов", body_text):
                has_special_offer = True
        elif _GIVEAWAY_WORD_REGEX.search(body_text) and _GIVEAWAY_SUB_REGEX.search(body_text):
            if not re.search(r"(?i)(?:сроки\s+проведения|организатор\s+розыгрыша|правила\s+акции)", body_text):
                has_special_offer = True

    has_banned_or_special = bool(has_crypto_banned or has_vpn_or_gambling or has_special_offer)
    return {
        "has_banned_or_special": has_banned_or_special,
        "has_crypto_banned": has_crypto_banned,
        "has_crypto_news_incident": has_crypto_news_incident,
        "has_vpn_or_gambling": has_vpn_or_gambling,
        "has_special_offer": has_special_offer,
    }


def _compute_brand_distribution(body_text: str, extracted_targets: Dict[str, Any]) -> Tuple[int, float]:
    """Counts unique domains/brands and computes top-brand mention share in [0, 1]."""
    domain_counts: Dict[str, int] = {}
    body_lower = body_text.lower()

    for u in extracted_targets.get("website_urls") or []:
        try:
            host = urllib.parse.urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
            if host and host not in _UGC_PLATFORM_HOSTS:
                stem = host.split(".")[0]
                cnt = max(1, body_lower.count(stem) if len(stem) >= 4 else 1)
                domain_counts[host] = max(domain_counts.get(host, 0), cnt)
        except Exception:
            continue

    for dom in extracted_targets.get("text_domains") or []:
        if dom not in _UGC_PLATFORM_HOSTS:
            stem = dom.split(".")[0]
            cnt = max(1, body_lower.count(stem) if len(stem) >= 4 else 1)
            domain_counts[dom] = max(domain_counts.get(dom, 0), cnt)

    # Also detect distinct proper-noun brands in text
    brand_counts: Dict[str, int] = dict(domain_counts)
    for w in re.findall(r"\b([A-Z][a-zA-Z0-9]{2,15})\b", body_text):
        if w.upper() in _NON_BRAND_UPPER_WORDS or not any(c.islower() for c in w):
            continue
        wl = w.lower()
        if any(wl in k for k in brand_counts):
            continue
        brand_counts[wl] = body_lower.count(wl)

    if not brand_counts:
        return 0, 0.0
    total_mentions = sum(brand_counts.values())
    top_mentions = max(brand_counts.values())
    return len(brand_counts), float(top_mentions / max(1, total_mentions))


class LinkCentricFeatureExtractor:
    """
    Extracts clean 48-dim Link-Centric feature vectors for LinkCentricAdClassifierV3
    using deepvk/USER2-base (or deterministic n-gram fallback in offline tests).
    """

    def __init__(
        self,
        device: str = "cpu",
        use_fast_ngram_fallback: bool = False,
    ):
        self.device = torch.device(device if (device == "cuda" and torch.cuda.is_available()) else "cpu")
        self.use_fast_ngram_fallback = use_fast_ngram_fallback
        self.encoder = None
        self.encoder_model_name = "ngram-256-fallback" if self.use_fast_ngram_fallback else "deepvk/USER2-base"
        if not self.use_fast_ngram_fallback:
            try:
                self.encoder = get_shared_user2_encoder(str(self.device))
            except Exception:
                self.use_fast_ngram_fallback = True
                self.encoder_model_name = "ngram-256-fallback"
        self._prototype_matrix: Optional[np.ndarray] = None

    def _get_prototype_matrix(self) -> np.ndarray:
        if self._prototype_matrix is not None:
            return self._prototype_matrix
        if self.encoder is not None and not self.use_fast_ngram_fallback:
            vecs = self.encoder.encode(
                list(SEMANTIC_LEGAL_PROTOTYPES_V3),
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).astype(np.float32)
        else:
            rows = [compute_ngram_hash_vector(t, dim=256) for t in SEMANTIC_LEGAL_PROTOTYPES_V3]
            vecs = np.stack(rows, axis=0).astype(np.float32)
            norms = np.linalg.norm(vecs, axis=-1, keepdims=True)
            vecs = vecs / np.maximum(norms, 1e-6)
        self._prototype_matrix = vecs
        return vecs

    def _encode_pairs(self, texts: List[str]) -> np.ndarray:
        if not texts:
            dim = int(self._get_prototype_matrix().shape[1])
            return np.zeros((0, dim), dtype=np.float32)
        if self.encoder is not None and not self.use_fast_ngram_fallback:
            return self.encoder.encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
                batch_size=32,
                show_progress_bar=False,
            ).astype(np.float32)
        rows = [compute_ngram_hash_vector(t, dim=256) for t in texts]
        vecs = np.stack(rows, axis=0).astype(np.float32)
        norms = np.linalg.norm(vecs, axis=-1, keepdims=True)
        return vecs / np.maximum(norms, 1e-6)

    def _compute_deterministic_part(self, doc: DocumentInput) -> Dict[str, Any]:
        text = doc.clean_text or ""
        body_text = strip_links_footer(text)
        is_own = resolve_is_own_website(doc)

        extracted_targets = extract_commercial_targets(
            text,
            raw_links=[doc.page_url] if doc.page_url else None,
            page_url=doc.page_url,
        )
        erid_tokens = extract_erid_tokens(
            text,
            page_url=doc.page_url,
            platform_ad_meta=doc.platform_ad_meta,
        )
        ad_label_status = analyze_ad_label_status(text)
        valid_inns = find_valid_inns_in_text(text)
        valid_ogrns = find_valid_ogrns_in_text(text)
        lang_168_status = detect_untranslated_foreign_words_168fz(body_text)

        comm_urls = list(extracted_targets["commercial_urls"])
        website_urls = list(extracted_targets["website_urls"])
        messenger_links = list(extracted_targets["messenger_links"])
        text_domains = list(extracted_targets["text_domains"])
        at_handles = list(extracted_targets["at_handles"])
        phones = list(extracted_targets["phones"])
        all_targets = list(extracted_targets["all_targets"])

        lead_window, link_window, anchor_windows, mean_rel_pos = extract_link_context_windows(
            text, extracted_targets, window_radius=350
        )

        has_aff_or_utm = any(
            COMMERCIAL_URL_PARAM_REGEX.search(u) or any(sh in u.lower() for sh in SHORTENER_HOSTS)
            for u in comm_urls
        )
        has_bot_or_max = any(
            re.search(r"(?i)(?:bot(?:\b|$|\?|/)|[?&]start=|max\.ru/)", u)
            for u in messenger_links
        )
        has_custom_path = any(has_custom_campaign_or_service_path(u) for u in comm_urls)

        has_promo_code = bool(
            _PROMO_CODE_OR_GIVEAWAY_REGEX.search(body_text)
            or (_GIVEAWAY_WORD_REGEX.search(body_text) and _GIVEAWAY_SUB_REGEX.search(body_text))
        )
        win_has_price = bool(_PRICE_DISCOUNT_REGEX.search(link_window))
        win_has_cta = bool(_CTA_IMPERATIVE_REGEX.search(link_window))
        win_has_pitch = bool(_SELF_PRODUCT_PITCH_REGEX.search(f"{lead_window}\n{link_window}"))
        win_has_superlative = bool(_SUPERLATIVE_REGEX.search(link_window))

        n_words = max(1.0, float(len(body_text.split())))
        doc_comm_hits = (
            len(_PRICE_DISCOUNT_REGEX.findall(body_text))
            + len(_CTA_IMPERATIVE_REGEX.findall(body_text))
            + len(_PROMO_CODE_OR_GIVEAWAY_REGEX.findall(body_text))
        )
        doc_comm_density = float(min(1.0, doc_comm_hits / max(1.0, n_words / 50.0)))

        # Unique commercial domains (excluding UGC platforms)
        unique_comm_hosts: Set[str] = set(d for d in text_domains if d not in _UGC_PLATFORM_HOSTS)
        for u in website_urls:
            try:
                h = urllib.parse.urlparse(u).netloc.lower().split(":")[0].removeprefix("www.")
                if h and h not in _UGC_PLATFORM_HOSTS:
                    unique_comm_hosts.add(h)
            except Exception:
                continue

        unique_brands_cnt, top_brand_dom = _compute_brand_distribution(body_text, extracted_targets)

        is_single_tg_footer = bool(
            len(messenger_links) == 1
            and not website_urls
            and not text_domains
            and not phones
            and not has_aff_or_utm
            and not has_bot_or_max
            and not has_promo_code
            and not win_has_price
            and not win_has_cta
        )

        banned_info = detect_banned_or_special_context(text, extracted_targets, is_own)
        has_banned_special = bool(banned_info["has_banned_or_special"])

        is_uniform_multibrand = bool(
            len(unique_comm_hosts) >= 3
            and not has_aff_or_utm
            and not has_bot_or_max
            and not has_promo_code
            and not has_banned_special
            and top_brand_dom <= 0.45
        )
        all_urls_editorial = bool(comm_urls) and all(is_editorial_crypto_news_url(u) for u in comm_urls)
        has_news_shield = bool(
            _EDITORIAL_NEWS_SHIELD_REGEX.search(body_text)
            or (all_urls_editorial and not has_aff_or_utm and not has_promo_code)
        )

        has_any_ad_label = bool(
            ad_label_status["has_strict_ad_label"]
            or ad_label_status["is_illegal_surrogate"]
            or (doc.platform_ad_meta or {}).get("is_promo")
        )
        has_commercial_channel = bool(
            all_targets
            or has_promo_code
            or has_any_ad_label
            or erid_tokens
            or win_has_cta
        )

        vec_det = np.zeros(28, dtype=np.float32)
        vec_det[0] = 1.0 if all_targets else 0.0
        vec_det[1] = float(min(1.0, math.log1p(len(unique_comm_hosts)) / 2.5))
        vec_det[2] = 1.0 if (website_urls or text_domains) else 0.0
        vec_det[3] = 1.0 if has_bot_or_max else 0.0
        vec_det[4] = 1.0 if has_aff_or_utm else 0.0
        vec_det[5] = 1.0 if has_custom_path else 0.0
        vec_det[6] = 1.0 if is_single_tg_footer else 0.0
        vec_det[7] = mean_rel_pos if all_targets else 0.0
        vec_det[8] = 1.0 if (text_domains or at_handles) else 0.0
        vec_det[9] = 1.0 if phones else 0.0
        vec_det[10] = float(min(1.0, unique_brands_cnt / 5.0))
        vec_det[11] = top_brand_dom
        vec_det[12] = 1.0 if has_promo_code else 0.0
        vec_det[13] = 1.0 if win_has_price else 0.0
        vec_det[14] = 1.0 if win_has_cta else 0.0
        vec_det[15] = 1.0 if win_has_pitch else 0.0
        vec_det[16] = 1.0 if win_has_superlative else 0.0
        vec_det[17] = doc_comm_density
        vec_det[18] = 1.0 if ad_label_status["has_strict_ad_label"] else 0.0
        vec_det[19] = 1.0 if ad_label_status["is_illegal_surrogate"] else 0.0
        vec_det[20] = 1.0 if (valid_inns or valid_ogrns) else 0.0
        vec_det[21] = 1.0 if erid_tokens else 0.0
        vec_det[22] = 1.0 if is_own else 0.0
        vec_det[23] = 1.0 if is_uniform_multibrand else 0.0
        vec_det[24] = 1.0 if has_news_shield else 0.0
        vec_det[25] = 1.0 if lang_168_status["has_violation_signals"] else 0.0
        vec_det[26] = 1.0 if has_banned_special else 0.0
        vec_det[27] = 1.0 if doc.published_days_ago >= 365 else 0.0

        tier0_flags = {
            "is_own_website": is_own,
            "has_erid": len(erid_tokens) > 0,
            "erid_tokens": erid_tokens,
            "ad_label_status": ad_label_status,
            "valid_inns": valid_inns,
            "valid_ogrns": valid_ogrns,
            "lang_168_status": lang_168_status,
            "has_affiliate": has_aff_or_utm,
            "has_promo_code": has_promo_code,
            "has_banned_special": has_banned_special,
            "banned_info": banned_info,
            "has_commercial_channel": has_commercial_channel,
            "is_statute_expired": doc.published_days_ago >= 365,
            "is_single_neutral_tg_footer": is_single_tg_footer,
            "is_uniform_multibrand_shield": is_uniform_multibrand,
        }
        return {
            "vec_det": vec_det,
            "lead_window": lead_window,
            "link_window": link_window,
            "anchor_windows": anchor_windows,
            "extracted_targets": extracted_targets,
            "tier0_flags": tier0_flags,
        }

    def extract_with_diagnostics(self, doc: DocumentInput) -> Dict[str, Any]:
        """Builds the (48,) feature vector and returns full diagnostic metadata."""
        det = self._compute_deterministic_part(doc)
        proto_mat = self._get_prototype_matrix()  # [10, D]
        pair_vecs = self._encode_pairs([det["lead_window"], det["link_window"]])  # [2, D]
        sims_lead = np.clip(pair_vecs[0] @ proto_mat.T, -1.0, 1.0).astype(np.float32)
        sims_link = np.clip(pair_vecs[1] @ proto_mat.T, -1.0, 1.0).astype(np.float32)

        features = np.concatenate([det["vec_det"], sims_lead, sims_link], axis=0).astype(np.float32)
        np.nan_to_num(features, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)

        return {
            "features": features,
            "lead_window": det["lead_window"],
            "link_window": det["link_window"],
            "anchor_windows": det["anchor_windows"],
            "extracted_targets": det["extracted_targets"],
            "tier0_flags": det["tier0_flags"],
        }

    def build_feature_vector(self, doc: DocumentInput) -> torch.Tensor:
        """Build [48] float32 feature tensor for a single document."""
        res = self.extract_with_diagnostics(doc)
        return torch.from_numpy(res["features"]).to(self.device)

    def build_batch_features(self, docs: List[DocumentInput]) -> torch.Tensor:
        """Build [Batch, 48] float32 feature tensor using batched GPU encoder calls."""
        if not docs:
            return torch.zeros((0, FEATURE_DIM), dtype=torch.float32, device=self.device)
        dets = [self._compute_deterministic_part(d) for d in docs]
        texts_to_encode: List[str] = []
        for d in dets:
            texts_to_encode.append(d["lead_window"])
            texts_to_encode.append(d["link_window"])

        proto_mat = self._get_prototype_matrix()  # [10, D]
        pair_vecs = self._encode_pairs(texts_to_encode)  # [2*B, D]
        sims = np.clip(pair_vecs @ proto_mat.T, -1.0, 1.0).astype(np.float32)  # [2*B, 10]

        rows: List[np.ndarray] = []
        for idx, d in enumerate(dets):
            lead_s = sims[2 * idx]
            link_s = sims[2 * idx + 1]
            row = np.concatenate([d["vec_det"], lead_s, link_s], axis=0).astype(np.float32)
            np.nan_to_num(row, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
            rows.append(row)

        batch_np = np.stack(rows, axis=0)
        return torch.from_numpy(batch_np).to(self.device)
