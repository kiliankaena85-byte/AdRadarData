# AdRadar Project Memory & State Passport

## Paths
- **Source Package**: `legal_sheaf_verifier/src/legal_sheaf/` and `legal_sheaf_verifier/src/ingestion/`.
- **Runtime & Reports**: Strictly outside OneDrive in `C:\AdRadarData\` (`reports\`, `leads\`, `cache\`).
- **Champion Neural Bundle**: `C:\AdRadarData\models\champion_v2.bundle` (LegalSheafNet-16x41).

## Toolchain
- **Python**: `.venv\Scripts\python.exe`.
- **Harness Supervisor**: All `.venv` synchronization, AST validation, and verification MUST run exclusively via:
  - `.venv\Scripts\python.exe harness.py sync`
  - `.venv\Scripts\python.exe harness.py probe <path>`
  - `.venv\Scripts\python.exe harness.py gate`

## Graveyard of Bugs (Запрещено повторять!)
1. **VC.ru API v2.8**: Use `https://api.vc.ru/v2.8/content?id={id}` (returns HTTP 200 with author and ad_data). The old `v2.1/subsite/0/content/{id}` returns 404.
2. **GIR BO FNS**: Extracting corporate revenue from `bo.nalog.gov.ru` requires header `Referer: https://bo.nalog.gov.ru/`.
3. **DaData Free Limits**: Free DaData returns `null` for `sites/emails/finance`. Do not expect financial metrics from it.
4. **Foreign KIO**: INN starting with `9909...` represents a foreign accredited entity (КИО), not a domestic Russian LLC.
5. **Zero-Mock & Zero-Simulation**: Absolute ban on `ProceduralCaseSimulator`, `random`, or synthetic generators in production modules.
6. **Self-Contained HTML**: Audit reports must never load external Google Fonts (`fonts.googleapis.com`) or foreign CDNs.
7. **CITATION LOCK**: Запрещено придумывать номера дел (`05-...`, `А40-...`) и писем (`ХХ/ХХХХХ/ХХ`). Разрешен только закрытый `verified_citations_whitelist.yaml`. Без номера — только статья КоАП/ФЗ!

## Active Queue
- [x] Foundation & Multi-layer Forensics (Axioms 1-12, Harness, supply chain resolver, 3-tier cascade, 105 tests).
- [x] Evolutionary Horizon Planner & Triad Decision Router (Sheaf + Laya + gemini-decision-1, 152-FZ PII firewall, WAL cache).
- [x] Commercial Productization & Turnkey Kit 14 900 ₽ (Russian Engineering Fintech web showcase, Lead DB, Oracle G Guard).
- [x] Autonomous Visual QC & Human Review Desk (`review_desk.html`, canvas stitcher, auto-uncheck, zero external CDNs).
- [x] Citation Lock & KoAP Guard (ч. 2 ст. 13.15, LiveCatalogResolver, CircuitBreaker, 12 RPM, SSRF route blocking, CSS PII blur).
- [x] Double Non-Autoregressive Lock (Vega-U Laya 0.4.32 on RTX 4060 37-42ms, Generative Cage, Oracle H certified).
- [x] i18n/Locale Trap & Clean Crawl (/ru/ prefix, Cyrillic ratio, DNS IP extraction, Phoenix clone under ч. 2 ст. 14.7).
- [x] Remediation Kit 14 900 ₽ deep personalization (Order 180 datacenter, 6-col matrix, 3-year consent, ZoZPP footer).
- [x] Pure-ASCII Launcher & Lawyer Dossier Exporter (CLI `export-lawyer-doc`, menu `[8]`, 7 sections, 138/138 tests).
- [x] Pre-Mortem Hardened Harness v2.0 (30-day RKN/FNS cache, Oracle I DOM & jargon guard, AST Code Map, hash 32ec1400d50b360b).
- [x] End-to-end audit of yulko.ru (ООО «ЮЛКО», Тверь, ИНН 6950149592, Уткин Ю.Ю., kit & report re-targeted, clean lead draft).
- [x] Dual-Track Multimodal Engine v2.0 (`vlm_context_engine`, Track A/B, ZDR, SSRF/PII blur, 152/152 tests).
- [x] Production Hardening & Operational Resilience (registry_cache 30d/7d, net_safety Anti-SSRF, batch_checkpoints, outreach_ledger 90d, AdRadar_Menu.bat, 160/160 tests, cert hash 46fb1df6218c6dd3).
- [x] GitHub Publishing & Documentation: pushed to https://github.com/kiliankaena85-byte/AdRadarData, hardened .gitignore, full usage guide & constraints in README.md, gate certified (160/160 tests, hash 46fb1df6218c6dd3).
- [ ] Next: VC.ru lead generation batch run via `AdRadar_Menu.bat`.
