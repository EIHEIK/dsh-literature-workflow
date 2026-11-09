---
name: literature-translation-review
description: Independently review the actual source and frozen CN/bilingual PDFs produced by literature_workflow, covering semantics, formulas, figures, tables and all output pages before submitting a candidate-bound report.
---

# 独立论文复核

必须在未参与该任务制作的真实 Harness 会话中运行。`review_begin` 从实际 exec session 注入身份并返回 ticket、模板、源文件、候选文件及预览路径。不能让制作会话伪造 reviewerId；没有独立会话能力就保持待审。

读取完整声明范围的源文和最终中文 PDF，按源页检查全文语义；查看每个中文和双语页面，复杂公式、图内小字用宿主实际图片能力放大核对。预览默认约 108 DPI，需要时自行以更高分辨率只读渲染当前绑定的 PDF。没有图像能力不得填视觉覆盖通过。

模板中的六个 checks 必须分别有依据：

- faithfulness：否定、限定、方法、结果及因果没有变义。
- complete_scope：摘要、正文、注释、实质附录及所需图表文字完整，不把部分试点称为全文。
- formulas：左端变量、等号、上下标、正负号、单位及编号完整，特别防裁切。
- figures：原图数据保留，所有有语义的英文标签有明确中文对应；可在定位清楚的附页。
- tables：表头和文字单元译出，数据、单位、行列关系和脚注正确。
- page_mapping：源页、译页、左右对照和续页对应真实。

填写实际查看过的 semanticSourcePages、visualCnPages、visualBiPages，保留模板中的 candidateId/sourceSha256/hashes。issues 每项使用稳定 id、detail、severity（blocker/major/minor/info）、status（open/closed）。描述具体位置和证据；不能因列表为空或文件可打开就宣布通过。

有未解决必修问题时 verdict=needs_attention，六项不能全部填 true。只有完整检查通过才能 pass。用 `review_submit` + 当前报告绝对路径提交。每个 ticket 一次提交；重新检查需新 ticket。内容修改后必须对新候选重新完整复核，旧通过不自动继承。

程序只校验证据身份与报告完整性，不能证明模型真的正确理解了科学内容。不要把离线合成测试报告当真实论文审查。
