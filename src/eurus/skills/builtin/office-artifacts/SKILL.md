---
name: office-artifacts
description: "Produce useful, well-structured Word documents and Excel workbooks with clear information hierarchy and reliable data handling. Describes what the Eurus Office Department engines expect from the artifact specification."
---
# Office Artifact Skill

Use for Word documents, spreadsheets, reports, proposals, and operational workbooks. This skill
advises the Office Department agents; it does not replace the renderers or the QA pipeline.

## How production works now

- The Main Agent delegates to the `office_specialist` tool. It never builds Office files itself.
- The Production Agent emits a structured JSON artifact specification (document, presentation, or
  workbook schema). The renderer converts that specification into the real file.
- Never hand raw Markdown to a Word renderer. Markdown input is parsed into structured blocks
  first; the DOCX engine renders native tables, native lists, and native formatting.
- Excel formulas belong in explicit `formula_columns`, `kpis`, or tables with `allow_formulas`
  set. Plain user text that looks like a formula is escaped for safety.

## Word documents

- Use a descriptive title, a short opening summary, and headings that describe the point of each
  section. Keep the heading hierarchy without skips (h1 then h2, not h1 then h3).
- Prefer short paragraphs, meaningful lists, and consistent terminology.
- Use tables for exact values, timelines for sequences, callouts for decisions or warnings, and
  quotes sparingly. Every table should have a short caption.
- Distinguish decisions, actions, risks, assumptions, and evidence.
- Preserve the author's intended meaning; mark unknown facts as needing verification rather than
  inventing details.

## Excel workbooks

- Make each worksheet answer a clear question. Sheet roles: overview, data, input, constants,
  calculations, results, dashboard, summary.
- Keep source values distinct from assumptions and calculated outputs. Explain metrics and date
  ranges. Dashboards need KPIs or charts; data sheets need headers, formats, freeze panes, and
  filters where useful.
- Avoid merged cells in data tables. Do not fabricate values to fill gaps. Treat user-provided
  text as text, not executable spreadsheet formulas.

## Delivery review

Check consistency, completeness, labels, spelling, units, and audience fit. Report any
assumptions or missing source data with the deliverable. The department's Critic, Visual QA, and
Client agents will reject work that fails the brief, leaks Markdown syntax, or clips content.
