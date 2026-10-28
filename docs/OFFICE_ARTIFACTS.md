# Office Artifacts

Eurus can create and read Word (`.docx`), Excel (`.xlsx`), and PowerPoint
(`.pptx`) files.

## Installation

```bash
python -m pip install -e ".[documents]"
```

## Tools

| Tool | Description |
| --- | --- |
| `document_read` | Reads `.docx`, `.xlsx`, and `.pptx` content inside the workspace |
| `document_create` | Creates a document from structured content; requires owner approval |
| `document_replace` | Replaces an existing document after a SHA-256 freshness check; requires approval |
| `excel_create` | Creates `.xlsx` workbooks with formulas, styles, and charts |
| `powerpoint_create` | Creates `.pptx` presentations with an HTML Canvas preview |
| `office_team_create` | Runs parallel proofreader, engagement-editor, and critical-review roles plus a lead producer |
| `office_replace` | Apply-style replacement for office files with freshness validation |

## Review workflow

The document team never writes the revision automatically. Reviewers produce
feedback, the lead editor synthesizes a proposal, and the actual write happens
only through the approval-gated creation or replacement tools. Replacement uses
a SHA-256 hash of the reviewed content, so a stale review cannot be applied over
a file that changed meanwhile.
