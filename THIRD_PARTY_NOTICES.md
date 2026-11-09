# Upstream interfaces and design references

This repository implements its own workflow; it does not redistribute third-party source trees, binaries, model weights, private papers or credentials.

- DeepSeek Harness documented tools and skills interfaces: https://github.com/deepseek-ai/deepseek-harness
- PDFMathTranslate-next / BabelDOC: separately installed subprocess engines; their respective AGPL-3.0 licenses continue to apply to those projects. https://github.com/PDFMathTranslate-next/PDFMathTranslate-next and https://github.com/funstory-ai/BabelDOC
- PyMuPDF: separately installed PDF runtime; see its upstream AGPL/commercial licensing. https://github.com/pymupdf/PyMuPDF
- `@deepseek-ai/schemastery`: declared npm dependency, not vendored. https://www.npmjs.com/package/@deepseek-ai/schemastery
- Zotero integrated MCP import interface: https://github.com/cookjohn/zotero-mcp
- Zotero selection/reading workflow reference: https://github.com/Eason-0712/dsh-zotero-skill
- Harness bundle/tool registration reference: https://github.com/Alex-yuanli/babeldoc-on-dsh

Documentation and source were read to understand interfaces and limitations. No upstream endorsement or certification is claimed. Test review identities and PDFs are synthetic fixtures.
