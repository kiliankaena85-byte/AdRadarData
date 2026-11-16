"""
SlotFeatureBuilder: Builds the (16, 36) feature tensor for LegalSheafNet-16x41.
Combines:
- Block A [0..7]: Deterministic Text & DOM Telemetry
- Block B [8..19]: Span-semantic features & context projections
- Block C [20..35]: Russian Legal Knowledge Base Precedent Matching
"""

from __future__ import annotations

import math
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
from bs4 import BeautifulSoup

from legal_sheaf.inn_erid_validators import (
    analyze_ad_label_status,
    detect_untranslated_foreign_words_168fz,
    extract_erid_tokens,
    find_valid_inns_in_text,
    find_valid_ogrns_in_text,
    validate_inn_checksum,
    validate_ogrn_checksum,
)
from legal_sheaf.precedent_index import LegalPrecedentIndex
from legal_sheaf.schema import DocumentInput, ParamIdx, SlotID


# =====================================================================
# SLOT CROSS-ATTENTION POOLER (BLOCK B: 14..19, 6 SEMANTIC DIMS)
# =====================================================================

class SlotCrossAttentionPooler(nn.Module):
    """
    Lightweight trainable cross-attention pooler.
    Queries token/text representations to output a 6-dim semantic projection
    for each of the 16 legal slots.
    """

    def __init__(self, num_slots: int = 16, hidden_dim: int = 256, out_dim: int = 6):
        super().__init__()
        self.num_slots = num_slots
        self.hidden_dim = hidden_dim
        self.out_dim = out_dim

        # Slot query embeddings
        self.slot_queries = nn.Parameter(torch.randn(num_slots, hidden_dim) * 0.02)
        self.proj = nn.Sequential(
            nn.Linear(hidden_dim, 64),
            nn.GELU(),
            nn.Linear(64, out_dim),
            nn.Tanh(),  # Bounded to [-1.0, 1.0]
        )

    def forward(self, text_embeds: torch.Tensor) -> torch.Tensor:
        """
        text_embeds: [Batch, SeqLen, hidden_dim] or [Batch, hidden_dim]
        Returns: [Batch, 16, 6]
        """
        if text_embeds.dim() == 2:
            # Expand to seq_len 1: [B, 1, H]
            text_embeds = text_embeds.unsqueeze(1)

        B = text_embeds.shape[0]
        # queries: [B, 16, H]
        queries = self.slot_queries.unsqueeze(0).expand(B, -1, -1)
        
        # Cross-attention: [B, 16, SeqLen]
        scale = 1.0 / math.sqrt(self.hidden_dim)
        attn_scores = torch.bmm(queries, text_embeds.transpose(1, 2)) * scale
        attn_weights = torch.softmax(attn_scores, dim=-1)
        
        # Context vectors: [B, 16, H]
        context = torch.bmm(attn_weights, text_embeds)
        out = self.proj(context)  # [B, 16, 6]
        return out


# =====================================================================
# DETERMINISTIC SLOT REGEXES & DETECTORS
# =====================================================================

SLOT_TRIGGERS: Dict[int, List[re.Pattern]] = {
    SlotID.OBJECT_IDENTITY: [
        re.compile(r"(?i)\b(?:бренд|компания|магазин|сервис|платформа|онлайн-школа|курс|продукт|товар|ооо|зао|пао|ао|ип|клиника|банк|агентство|предприятие|комбинат|завод|фабрика)\b"),
        re.compile(r"[«\"'`][А-Яа-яA-Za-z0-9_\-\s]{2,40}[»\"'`]"),
        re.compile(r"\b[A-Z][a-zA-Z0-9_\-]{2,20}\b"),
    ],
    SlotID.ATTENTION_FOCUS: [
        re.compile(r"(?i)\b(?:лучш(?:ий|ая|ее|ие|их)|№\s*1|номер\s+один|самый\s+\w+|топов(?:ый|ая|ое|ые)|уникальн(?:ый|ая|ое)|инновационн(?:ый|ая|ое)|гарантированн(?:ый|о)|лидер\s+рынка|главная\s+акция|супер-?\w+|только\s+сегодня|непревзойденн(?:ый|ое))\b"),
    ],
    SlotID.COMMERCIAL_TERMS: [
        re.compile(r"(?i)\b(?:цена|стоимость|тариф|руб(?:лей|\.)?|₽|скидк[аеиу]|скидо?к|\d+%\s*скидк|\d+\s*руб|акция|выгод[ае]|распродаж[ае]|дедлайн|спеццена|бесплатн(?:о|ый|ое|ом)|выручка|годовой\s+отчет)\b"),
    ],
    SlotID.CALL_TO_ACTION: [
        re.compile(r"(?i)\b(?:купи(?:те)?|заказывай(?:те)?|переходи(?:те)?|регистрируй(?:тесь)?|подписывай(?:тесь)?|забирай(?:те)?|жми(?:те)?|оформляй(?:те)?|запись\s+на\s+прием|скачивай(?:те)?|приглашает|приходите|подайте\s+онлайн-заявку|промокод[:\s]*\w+)\b"),
    ],
    SlotID.LINK_TOPOLOGY: [
        re.compile(r"https?://[^\s<>\"']+"),
        re.compile(r"(?i)(?:utm_source|utm_campaign|utm_medium|partner|affiliate|ref=)"),
    ],
    SlotID.AD_LABEL_MARK: [
        re.compile(r"(?i)\b(?:реклама|рекламный\s+материал)\b"),
        re.compile(r"(?i)\b(?:партнерский\s+материал|промо|спецпроект|при\s+поддержке|спонсорский\s+контент)\b"),
    ],
    SlotID.ADVERTISER_INFO: [
        re.compile(r"(?i)\b(?:инн\s*[:\s]*\d{10,12}|огрн\s*[:\s]*\d{13,15}|ооо|пао|ао|зао|ип\s+[а-яё]+)\b"),
    ],
    SlotID.ERID_INTEGRITY: [
        re.compile(r"(?i)\b(?:erid[:\s=]*[a-zA-Z0-9_\-]{8,64}|токен(?:\s+маркировки)?[:\s=]*[a-zA-Z0-9_\-]{8,64})\b"),
    ],
    SlotID.SUBJECT_RELATION: [
        re.compile(r"(?i)\b(?:наш\s+(?:сайт|магазин|ассортимент|продукт|каталог|фирменный)|мы\s+(?:рады|предлагаем|разработали)|авторск(?:ий|ая)|партнер(?:ск|ом))\b"),
    ],
    SlotID.ANALYTICAL_SHIELD: [
        re.compile(r"(?i)\b(?:исследовани[ея]|методологи[яи]|эксперимент|аналитик[аеиу]|аналитическ(?:ий|ая|ое|их|ом)|научн(?:ый|ая|ое)|статистик[аеи]|справочн(?:ый|ая|ое)|результаты\s+тестов|отчет|обзор(?:\s+рынка)?|образовательн(?:ый|ое)|пресс-релиз)\b"),
    ],
    SlotID.MULTI_BRAND_REVIEW: [
        re.compile(r"(?i)\b(?:сравнительн(?:ый|ая|ое|ых)(?:\s+\w+)?\s+обзор|сравнени[ея]|сравнительн(?:ый|ая|ое|о)|дайджест|плюсы\s+и\s+минусы|сводная\s+таблица|несколько\s+решений|конкурирующ(?:их|ие)|аналоги|без\s+выделения\s+фаворита)\b"),
    ],
    SlotID.ORGANIC_INTEGRATION: [
        re.compile(r"(?i)\b(?:я\s+сам\s+пользуюсь|мой\s+личный\s+опыт|в\s+моем\s+случае|дел(?:юсь|иться)(?:\s+впечатлениями)?|в\s+процессе\s+работы|заметил|останавливались|понравил(?:ось|ась|ся)|путешестви[ия]|поездк[аеи]|отдых[ае]|впечатлени[яеи]|красив(?:ый|ая|ое|ыми))\b"),
    ],
    SlotID.SELF_CATALOG_NORM: [
        re.compile(r"(?i)\b(?:каталог|ассортимент|в\s+наших\s+магазинах|поступление|поступили|график\s+работы|новости\s+компании|режим\s+работы|в\s+наличии)\b"),
    ],
    SlotID.RUSSIAN_LANG_168FZ: [
        re.compile(r"(?i)\b(?:sale|black\s+friday|discount|cashback|special\s+offer|free|exclusive|best\s+price|grand|mega)\b"),
    ],
    SlotID.SPECIAL_BANNED: [
        re.compile(r"(?i)\b(?:vpn|обход\s+блокировок|казино|ставки|букмекер|криптовалют|доходность\s+\d+%|кредит\s+под\s+0%|противопоказания|розыгрыш\s+призов|клиник[аеиу]|лазерн(?:ое|ого)|омоложени[ея]|контурн(?:ой|ая)\s+пластик|медицин(?:ы|ск)|эстетическ(?:ой|ая)\s+медицин)\b"),
    ],
    SlotID.PROCEDURAL_KOAP: [
        re.compile(r"(?i)\b(?:срок\s+давности|мсп|малый\s+бизнес|микропредприяти[ея]|микробизнес|предупреждение|ст\.?\s*4\.1\.1|ст\.?\s*4\.5|ст\.?\s*14\.3|старый\s+пост)\b"),
    ],
}


class SlotFeatureBuilder:
    """
    Builds the complete state tensor of shape (Batch, 16, 36) for LegalSheafNet.
    Block A (0..7): Deterministic & DOM telemetry
    Block B (8..19): Span-semantics & Cross-Attention projection
    Block C (20..35): Legal Precedent Matching
    """

    def __init__(
        self,
        precedent_index: Optional[LegalPrecedentIndex] = None,
        device: str = "cpu",
        use_fast_ngram_fallback: bool = False,
    ):
        self.device = torch.device(device if (device == "cuda" and torch.cuda.is_available()) else "cpu")
        self.precedent_index = precedent_index or LegalPrecedentIndex(
            device=str(self.device),
            use_fast_ngram_fallback=use_fast_ngram_fallback,
        )
        self.semantic_pooler = SlotCrossAttentionPooler(num_slots=16, hidden_dim=256, out_dim=6).to(self.device)
        self.semantic_pooler.eval()

    def _extract_dom_elements(self, raw_html: str) -> List[Tuple[str, str, float]]:
        """
        Parse HTML and return list of (text_content, tag_name, salience_weight).
        Tag salience weights:
        h1-h3: 1.0, button/cta_a: 0.9, b/strong/blockquote: 0.7, p/li: 0.35, small/footer: 0.15.
        """
        if not raw_html.strip():
            return []

        elements = []
        try:
            soup = BeautifulSoup(raw_html, "html.parser")
            # Remove scripts and styles
            for s in soup(["script", "style", "noscript"]):
                s.decompose()

            for tag in soup.find_all(True):
                tag_name = tag.name.lower()
                text = tag.get_text(separator=" ", strip=True)
                if not text:
                    continue

                if tag_name in ("h1", "h2", "h3"):
                    weight = 1.0
                elif tag_name in ("button",) or (tag_name == "a" and any(c in tag.get("class", []) for c in ["btn", "cta", "button"])):
                    weight = 0.9
                elif tag_name in ("b", "strong", "blockquote"):
                    weight = 0.7
                elif tag_name in ("p", "li", "span", "div"):
                    weight = 0.35
                elif tag_name in ("small", "footer"):
                    weight = 0.15
                else:
                    weight = 0.25

                elements.append((text, tag_name, weight))
        except Exception:
            pass

        return elements

    def _compute_gini(self, counts: List[int]) -> float:
        """Compute Gini concentration index of counts across paragraphs."""
        if not counts or sum(counts) == 0:
            return 0.0
        n = len(counts)
        if n == 1:
            return 1.0
        arr = np.array(sorted(counts), dtype=np.float32)
        idx = np.arange(1, n + 1)
        gini = (2.0 * np.sum(idx * arr) - (n + 1) * np.sum(arr)) / (n * np.sum(arr))
        return float(np.clip(gini, 0.0, 1.0))

    def build_document_tensor(self, doc: DocumentInput) -> torch.Tensor:
        """
        Build (16, 36) tensor for a single document.
        Returns: torch.Tensor of shape (16, 36) on self.device.
        """
        text = doc.clean_text
        text_len = max(1, len(text))
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        if not paragraphs:
            paragraphs = [text]

        # Extract DOM elements
        dom_elements = self._extract_dom_elements(doc.raw_html)

        # Extract valid entities
        valid_inns = find_valid_inns_in_text(text)
        valid_ogrns = find_valid_ogrns_in_text(text)
        erid_tokens = extract_erid_tokens(text, page_url=doc.page_url, platform_ad_meta=doc.platform_ad_meta)
        ad_label_status = analyze_ad_label_status(text)
        lang_168_status = detect_untranslated_foreign_words_168fz(text)

        # Context mode resolution (OWN_WEBSITE vs EXTERNAL_PUBLICATION)
        is_own_website = False
        if doc.context_mode == "OWN_WEBSITE":
            is_own_website = True
        elif doc.context_mode == "AUTO":
            if doc.author_domain and doc.page_url:
                p_net = urllib.parse.urlparse(doc.page_url).netloc.lower()
                a_net = doc.author_domain.lower()
                if a_net in p_net or p_net in a_net:
                    is_own_website = True
            elif doc.author_domain:
                is_own_website = True

        # Links in document
        links = re.findall(r"https?://[^\s<>\"']+", text)
        has_external_links = False
        author_domain = doc.author_domain.lower().strip()
        for link in links:
            parsed = urllib.parse.urlparse(link)
            netloc = parsed.netloc.lower()
            if author_domain and author_domain not in netloc:
                has_external_links = True
                break
        if not links and doc.page_url:
            links = [doc.page_url]

        # Slot texts for precedent matching (Block C)
        slot_texts: Dict[int, str] = {}
        for s_id in range(16):
            slot_texts[s_id] = ""

        # Pre-allocate output matrix (16, 36)
        matrix = np.zeros((16, 36), dtype=np.float32)

        # Loop through each slot to compute Block A (0..7) and Block B (8..13)
        for s_id in range(16):
            patterns = SLOT_TRIGGERS.get(s_id, [])
            match_spans = []
            slot_snippet_list = []

            for p in patterns:
                for m in p.finditer(text):
                    match_spans.append((m.start(), m.end()))
                    slot_snippet_list.append(m.group(0))

            count = len(match_spans)
            slot_texts[s_id] = " ".join(slot_snippet_list) if slot_snippet_list else text[:250]

            # -------------------------------------------------------------
            # BLOCK A: Telemetry (0..7)
            # -------------------------------------------------------------
            trigger_present = 1.0 if count > 0 else 0.0
            trigger_log_freq = min(1.0, math.log1p(count) / 4.0)

            if count > 0:
                first_pos_rel = match_spans[0][0] / float(text_len)
                last_pos_rel = match_spans[-1][1] / float(text_len)
                total_span_chars = sum(end - start for start, end in match_spans)
                char_coverage_ratio = min(1.0, total_span_chars / float(text_len))
            else:
                first_pos_rel = 0.0
                last_pos_rel = 0.0
                char_coverage_ratio = 0.0

            # DOM salience
            dom_salience = 0.25
            if count > 0 and dom_elements:
                for snip in slot_snippet_list:
                    for elem_text, _, weight in dom_elements:
                        if snip.lower() in elem_text.lower():
                            dom_salience = max(dom_salience, weight)

            # Link proximity
            if count > 0 and links:
                # Find min character distance between any trigger and any link
                min_dist = 999999
                for start, end in match_spans:
                    for l_match in re.finditer(r"https?://[^\s<>\"']+", text):
                        d = min(abs(start - l_match.end()), abs(l_match.start() - end))
                        min_dist = min(min_dist, d)
                token_dist = min_dist / 6.0  # Approx 6 chars per token
                link_prox = math.exp(-token_dist / 40.0)
            else:
                link_prox = 0.0

            # Deterministic checksum score (Param 7)
            det_score = 0.0
            if s_id == SlotID.ADVERTISER_INFO:
                det_score = 1.0 if (len(valid_inns) > 0 or len(valid_ogrns) > 0 or is_own_website) else 0.0
            elif s_id == SlotID.ERID_INTEGRITY:
                if is_own_website and not has_external_links:
                    # Own catalog / public offer is immune from ERID (38-FZ Art. 2 Part 2, FAS Letter AT/61279/26)
                    det_score = 1.0
                else:
                    det_score = 1.0 if len(erid_tokens) > 0 else 0.0
            elif s_id == SlotID.SELF_CATALOG_NORM:
                det_score = 1.0 if is_own_website else (1.0 if trigger_present > 0.5 else 0.0)
            elif s_id == SlotID.AD_LABEL_MARK:
                det_score = 1.0 if ad_label_status["has_strict_ad_label"] else (-1.0 if ad_label_status["is_illegal_surrogate"] else 0.0)
            elif s_id == SlotID.RUSSIAN_LANG_168FZ:
                det_score = 1.0 if lang_168_status["has_violation_signals"] else 0.0
            elif s_id == SlotID.PROCEDURAL_KOAP:
                # If published > 365 days ago, statute of limitations expired!
                det_score = 1.0 if doc.published_days_ago >= 365 else 0.0
            elif s_id == SlotID.SUBJECT_RELATION:
                det_score = 1.0 if (is_own_website or (author_domain and not has_external_links)) else 0.0
            elif s_id == SlotID.MULTI_BRAND_REVIEW:
                # Check for >= 3 brands
                brand_count = len(re.findall(r"[«\"][А-Яа-яA-Za-z0-9_\-\s]{3,20}[»\"]", text))
                det_score = 1.0 if brand_count >= 3 else min(1.0, brand_count / 3.0)
            else:
                det_score = 1.0 if trigger_present > 0.5 else 0.0

            matrix[s_id, 0] = trigger_present
            matrix[s_id, 1] = trigger_log_freq
            matrix[s_id, 2] = first_pos_rel
            matrix[s_id, 3] = last_pos_rel
            matrix[s_id, 4] = char_coverage_ratio
            matrix[s_id, 5] = dom_salience
            matrix[s_id, 6] = link_prox
            matrix[s_id, 7] = det_score

            # -------------------------------------------------------------
            # BLOCK B: Spans & Context (8..13)
            # -------------------------------------------------------------
            span_conf = min(1.0, 0.5 + 0.1 * count) if count > 0 else 0.0
            matrix[s_id, 8] = span_conf   # max conf
            matrix[s_id, 9] = span_conf   # mean conf

            # Gini concentration across paragraphs
            para_counts = []
            for p in paragraphs:
                p_cnt = sum(len(rgx.findall(p)) for rgx in patterns)
                para_counts.append(p_cnt)
            matrix[s_id, 10] = self._compute_gini(para_counts)

            # Imperative, superlative, numeric offer density in slot window
            target_window = slot_texts[s_id] if count > 0 else text[:400]
            w_words = max(1, len(target_window.split()))
            
            imp_count = len(re.findall(r"(?i)\b(?:купи|закажи|переходи|регистрируйся|подпишись|жми|забирай|оформи)\b", target_window))
            sup_count = len(re.findall(r"(?i)\b(?:лучш\w+|№\s*1|топов\w+|самый|уникальн\w+|номер\s+один)\b", target_window))
            num_count = len(re.findall(r"(?:%|\d+\s*(?:руб|₽|долл|\$)|скидк|цена|тариф)", target_window))

            matrix[s_id, 11] = min(1.0, (imp_count / float(w_words)) * 5.0)
            matrix[s_id, 12] = min(1.0, (sup_count / float(w_words)) * 5.0)
            matrix[s_id, 13] = min(1.0, (num_count / float(w_words)) * 5.0)

        # -------------------------------------------------------------
        # BLOCK B: Semantic Projections (14..19, 6 dims)
        # -------------------------------------------------------------
        # Generate text embedding vector using n-gram hash or pooler
        q_vec = self.precedent_index._compute_ngram_hash_vector(text, dim=256)
        q_tensor = torch.from_numpy(q_vec).unsqueeze(0).to(self.device)  # [1, 256]
        with torch.no_grad():
            semantic_projs = self.semantic_pooler(q_tensor)  # [1, 16, 6]
            semantic_projs_np = semantic_projs.squeeze(0).cpu().numpy()  # [16, 6]

        matrix[:, 14:20] = semantic_projs_np

        # -------------------------------------------------------------
        # BLOCK C: Russian Legal KB Precedent Matching (20..35, 16 dims)
        # -------------------------------------------------------------
        block_c = self.precedent_index.compute_all_slots_block_c(slot_texts, target_inn=doc.inn)
        matrix[:, 20:36] = block_c

        # Ensure no NaN / Inf and clip values
        np.nan_to_num(matrix, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)
        
        return torch.from_numpy(matrix).to(self.device)

    def build_batch_tensor(self, docs: List[DocumentInput]) -> torch.Tensor:
        """
        Build (Batch, 16, 36) tensor for a batch of documents.
        """
        tensors = [self.build_document_tensor(d) for d in docs]
        return torch.stack(tensors, dim=0)
