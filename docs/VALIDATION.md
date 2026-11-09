# Validation status

## 0.2.0-beta.1 — 2026-11-10

This is a controlled test release, not certification of paper translation quality.

- Python: 217 tests passed, including public-package privacy checks; Node: 5 tests passed. Coverage includes budgets, fixed-scope authorization, native text order, image-bound source evidence, bounded repairs, restoration, artifact checks, independent review and publication.
- A real isolated container fixture completed negative initial review, one semantic repair, full fresh review, publication and simulated Zotero lost-response reconciliation. The improved inline repair kept this two-source-page fixture at 3 Chinese/bilingual pages instead of 5. Provider and Zotero responses in this fixture were synthetic; no external API calls or real Zotero writes.
- Real DeepSeek API translation and review were exercised. The first real paper remained unaccepted after two repair rounds, with a current-candidate issue bundle preserved. The exact originally authorized remaining 14 papers were started under a scoped maintenance receipt. This does not set general `productionReady` to true.
- A real Zotero source index was built with versioned native paragraph extraction. Roles, the local service, the DSH Node adapter and the registered login bootstrap command were exercised. A real Windows reboot/login trigger was not tested.
- No real accepted full-paper delivery or real Zotero attachment write has been verified for this beta. The official native write bridge still requires an explicit one-time authorization. DSH cold installation, general new-machine installation and public-source execution remain incomplete.
- API credentials, private configuration, original papers, job data and local operational handoff documents are excluded from the public package.

## Historical RC1 evidence

Version: 0.1.0-rc.1. Date: 2026-11-09.

This file records development evidence, not acceptance of translated research papers.

- Python tests: **25 passed**, Windows / Python 3.12 / PyMuPDF 1.28.2. Synthetic PDF/SQLite/worker/proxy/Zotero fixtures; real subprocess crash and competing claim tests are included.
- Node tests: **3 passed**, Node 24.22.1. Documented tool and skills provider contracts with a mock host; no live DSH installation is implied.
- Entry module: imported with the actual declared schemastery dependency and default configuration evaluated.
- PDF composition: synthetic Chinese/English formula and figure-label layout rendered and visually inspected. Corrected mixed-font spacing in the supplement and verified the original formulas remain visible in the left panel.
- No real DeepSeek API request or production Zotero mutation during development.
- Live DSH installation, current engine package integration, real model translation and actual Zotero attachment readback remain to be tested by the user in DSH.

The release package SHA-256 is supplied in SHA256SUMS.txt. Do not infer live compatibility from a passing mocked interface test. Windows filesystem sandbox restrictions required these native-PDF tests to run outside that sandbox in an isolated project test directory; no ACL or production data was changed.
