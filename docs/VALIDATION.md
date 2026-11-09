# Validation status

Version: 0.1.0-rc.1. Date: 2026-11-09.

This file records development evidence, not acceptance of translated research papers.

- Python tests: **25 passed**, Windows / Python 3.12 / PyMuPDF 1.28.2. Synthetic PDF/SQLite/worker/proxy/Zotero fixtures; real subprocess crash and competing claim tests are included.
- Node tests: **3 passed**, Node 24.22.1. Documented tool and skills provider contracts with a mock host; no live DSH installation is implied.
- Entry module: imported with the actual declared schemastery dependency and default configuration evaluated.
- PDF composition: synthetic Chinese/English formula and figure-label layout rendered and visually inspected. Corrected mixed-font spacing in the supplement and verified the original formulas remain visible in the left panel.
- No real DeepSeek API request or production Zotero mutation during development.
- Live DSH installation, current engine package integration, real model translation and actual Zotero attachment readback remain to be tested by the user in DSH.

The release package SHA-256 is supplied in SHA256SUMS.txt. Do not infer live compatibility from a passing mocked interface test. Windows filesystem sandbox restrictions required these native-PDF tests to run outside that sandbox in an isolated project test directory; no ACL or production data was changed.
