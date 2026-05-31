---
name: style-google-doc
description: Apply the standard Google Doc visual style (typography, colors, spacing) to a document. Use when the user says "/style-google-doc" or wants to reformat a Google Doc.
argument-hint: <google-doc-url-or-id>
allowed-tools: [mcp__google-docs__listTabs, mcp__google-docs__readDocument, mcp__google-docs__applyTextStyle, mcp__google-docs__applyParagraphStyle, mcp__google-docs__getTable, mcp__google-docs__listTables]
---

# Style Google Doc

**Tool constraint:** ALL document operations MUST use the `mcp__google-docs__*` tools (the Google Docs GCP MCP server). Never use WebFetch, raw HTTP calls, or any other approach to read or modify documents.

The user invoked this with: $ARGUMENTS

## Step 1: Parse the document ID

Extract the document ID from `$ARGUMENTS`. It may be:
- A raw ID: `1cRcRRnuWQQqkvq48EIIdC7RrsqxIx7kNzYeUBj6uE44`
- A full URL: `https://docs.google.com/document/d/<ID>/edit`

Extract just the ID (the segment between `/d/` and the next `/`).

If `$ARGUMENTS` is empty or unparseable, ask the user: "Please provide a Google Doc URL or document ID."

## Step 2: Discover tabs

Call `mcp__google-docs__listTabs` on the document. If the document has multiple tabs, you will style each tab in turn. If it has only one tab (or no tabs field), treat it as a single-tab document.

For each tab, note its `tabId`. All subsequent read and style calls must pass that `tabId` — character indices are per-tab, not per-document.

## Step 3: Read and style each tab

For each tab (pass `tabId` to every call):

Call `mcp__google-docs__readDocument` with `format: "json"` and the tab's `tabId` to retrieve its full structure — every paragraph's `namedStyleType` and character ranges.

Walk every paragraph in that tab's body. For each paragraph, apply the following styles based on its `namedStyleType`.

### Named style definitions

Apply these using `mcp__google-docs__applyTextStyle` (for font/size/color) and `mcp__google-docs__applyParagraphStyle` (for spacing).

#### NORMAL_TEXT
- Font: Arial, 11pt, color #000000
- Space above: 0pt, space below: 0pt
- Line spacing: 115%

#### TITLE
- Font: Arial, 26pt, color #1F4E79
- Space above: 0pt, space below: 3pt
- Apply color as an inline text style override (not just named style)

#### SUBTITLE
- Font: Arial, 15pt, color #666666, not italic
- Space above: 0pt, space below: 16pt

#### HEADING_1
- Font: Arial, 16pt, color #1F4E79
- Space above: 20pt, space below: 12pt
- Note: H1 bottom border (#1F4E79, 1pt, 6pt padding) cannot be set via MCP — flag for manual touch-up

#### HEADING_2
- Font: Arial, 14pt, color #000000
- Space above: 18pt, space below: 6pt

#### HEADING_3
- Font: Arial, 14pt, color #434343, not bold
- Space above: 16pt, space below: 4pt

#### HEADING_4
- Font: Arial, 12pt, color #666666
- Space above: 14pt, space below: 4pt

#### HEADING_5
- Font: Arial, 11pt, color #666666
- Space above: 12pt, space below: 4pt

#### HEADING_6
- Font: Arial, 11pt, color #666666, italic
- Space above: 12pt, space below: 4pt

### Applying styles efficiently

For each paragraph:
1. Determine its `namedStyleType`
2. Call `mcp__google-docs__applyTextStyle` with the paragraph's start/end index, the correct font family (`Arial`), font size (in pt), and foreground color (hex)
3. Call `mcp__google-docs__applyParagraphStyle` with the same index range and the correct `spaceAbove` and `spaceBelow` values (in pt)
4. For NORMAL_TEXT, also set line spacing to 115%
5. For SUBTITLE, explicitly set `italic: false`
6. For HEADING_3, explicitly set `bold: false`

Batch these calls efficiently — process all paragraphs, tracking counts by style type.

## Step 4: Apply table styles (what's possible)

For each tab, use `mcp__google-docs__listTables` (with `tabId`) to find all tables, then `mcp__google-docs__getTable` (with `tabId`) to inspect rows and cells.

For table cell text, apply via `mcp__google-docs__applyTextStyle` using the cell's character range:

| Row | Font | Size | Color | Bold |
|---|---|---|---|---|
| Row 0 (header) | Arial | 10pt | #1F4E79 | yes |
| Rows 1+ (odd) | Arial | 10pt | #333333 | no |
| Rows 1+ (even) | Arial | 10pt | #333333 | no |

Note: Cell background colors (header #EBF1F6, stripe #F4F6FA) and cell padding/borders cannot be set via MCP — flag for manual touch-up.

## Step 5: Report results

After processing all tabs, report a summary:

```
Tabs processed: <n> (Tab Name 1, Tab Name 2, ...)

Applied styles to <N> paragraphs (across all tabs):
  NORMAL_TEXT: <n>
  HEADING_1:   <n>
  HEADING_2:   <n>
  HEADING_3:   <n>
  ...
  TITLE:       <n>
  SUBTITLE:    <n>

Table cells styled: <n> tables, <n> cells total

⚠️  Requires manual touch-up (MCP limitations):
  - HEADING_1 bottom border: #1F4E79, 1pt, 6pt padding
  - Table header row background: #EBF1F6
  - Table alternating row background: #F4F6FA (odd rows)
  - Table cell padding: 5pt all sides
  - Table cell borders: none (borderless)
```

---

## Style reference (inline text patterns)

These are guidance for the user — the skill applies named-style-level formatting only. Inline detection requires semantic understanding of content.

| Pattern | Color | Size | Modifiers |
|---|---|---|---|
| Bold section labels | #1F4E79 | 11pt | bold |
| Body text | #333333 | 11pt | — |
| Blockquotes | #333333 | 11pt | italic |
| Small annotations | #333333 | 10pt | — |
| Parenthetical refs | #666666 | 10pt | italic |

## Color palette

| Name | Hex | Usage |
|---|---|---|
| Dark blue (accent) | #1F4E79 | H1, Title, bold labels, H1 border, table headers |
| Body dark | #333333 | Body text, quotes, annotations, table data |
| Dark gray | #434343 | H3 |
| Medium gray | #666666 | H4–H6, Subtitle, parenthetical refs |
| Black | #000000 | H2, fallback |
| Header row bg | #EBF1F6 | Table header row background (manual) |
| Stripe row bg | #F4F6FA | Table alternating row background (manual) |
