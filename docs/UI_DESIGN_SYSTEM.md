# RenWeave desktop design system

RenWeave uses an **Aurora Workbench** system: Windows 11–informed component rendering with Material 3 semantic states for a focused desktop workflow. This document is normative: new UI code must use these tokens and component roles rather than creating page-specific widget styling.

## Reference basis

- [Material Design 3](https://m3.material.io/) informs semantic color roles, component states, hierarchy, and accessible interaction feedback.
- [Sun Valley ttk theme](https://github.com/rdbende/Sun-Valley-ttk-theme) provides the bundled rounded Windows component layer.
- [UI UX Pro Max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) informs the explicit design-system workflow, responsive checks, and pre-delivery visual audit.
- [Awesome Design Systems](https://github.com/alexpate/awesome-design-systems) was used as a catalog of mature system patterns rather than as a source of page-specific styling.

Material Design 3, UI UX Pro Max, and Awesome Design Systems are design references rather than runtime dependencies. Sun Valley ttk is bundled as a runtime dependency. RenWeave's Tk composition, desktop workflow, tokens, and Aurora Workbench identity are project-specific.

## Layout grid

- Default window: `1240 × 840`; minimum: `900 × 640`.
- Workflow rail: `232 px` by default and a compact `76 px` rail below the narrow breakpoint.
- Main canvas horizontal inset: `36 px` by default and `24 px` in compact layouts.
- Card inset: `18 px`; standard field gap: `6 px`.
- Spacing uses the shared 4/8/12/16/20/24 scale from `Metrics`.
- Interactive controls target a `44 px` visual height and use Microsoft YaHei UI for bilingual consistency.
- Typography uses exactly four size tokens: `SMALL` (9) for metadata and hints, `BODY` (10) for controls and ordinary copy, `TITLE` (18) for dialog/brand emphasis, and `DISPLAY` (24) for page titles and primary progress values. Weight communicates hierarchy within those sizes; page code must not introduce numeric font sizes.
- Page titles, body copy, cards, fields, and activity content share one left edge.
- Text and form content is left-aligned. Center alignment is reserved for button labels and compact numeric status.
- Pages should remain structurally fixed within the minimum window. The outer canvas is an overflow fallback, not the normal reading surface; long diagnostics, incremental-scope details, and logs belong in bounded local scroll regions.

## Action placement

| Context | Placement |
| --- | --- |
| Interface language | One top-right button beside Settings; its label names the language it will switch to |
| Section-wide auxiliary action | Top-right of that section |
| Field-specific action | Attached to the right of its field, equal control height |
| Form action | Left edge of its form column |
| Dialog actions | Bottom-right, secondary before primary |
| Workflow actions | Standalone back and primary buttons on the page surface with a stable explanatory center column |

The workflow action row never changes its column geometry between pages. It must not look like a full-width colored bar. Empty slots remain reserved so explanatory text and the main action do not jump when navigating.

## Components

All interactive widgets come from `RenWeaveDesktopApp` component factories or one of the two documented navigation/provider styles.

| Component | Required implementation | Rules |
| --- | --- | --- |
| Primary button | `_button(..., kind="primary")` | One per action region; fixed height and standard width |
| Secondary button | `_button(..., kind="secondary")` | Back, cancel, pause, and non-destructive alternatives |
| Field action | `_button(..., kind="field")` | Browse, choose, copy, and attached field actions |
| Text field | `_entry(...)` | Shared padding, border, focus, disabled, selection, and typography |
| Combo box | `_combobox(...)` | Same visual height as text fields; editable unless explicitly read-only |
| Checkbox | `Material.TCheckbutton` | Left-aligned with the form text column |
| Model list | `ModelList.Treeview` | `38 px` rows, single selection, consistent selected state |
| Provider gallery | `_layout_provider_buttons()` | All provider presets stay visible in a stable grid; there is no fold control or height animation |
| Translation activity | determinate + indeterminate progress pair | Show overall completion separately from continuous worker activity, exact pipeline stage, and high-level phase state |
| Vertical scroll | `_scrollbar(...)` | Shared narrow track, thumb, hover, and arrow treatment |
| Diagnostic/long text | bounded styled `tk.Text` | Fixed-height local scrolling with the same border/focus/selection colors as form controls; do not create dozens of wrapping labels that enlarge the whole page |
| Completed outputs | secondary actions on the progress page and completion dialog | Always offer the RPY directory; offer the RPA-containing directory when an archive was generated |
| Outer card | `Card.TFrame` | The only bordered content container |
| Inner layout | `CardBody.TFrame` | No nested pseudo-card borders |

## Interaction states

- Every clickable control has hover, pressed, disabled, and keyboard-focus states. Focus is a solid accent border/state contrast; dotted focus rectangles are prohibited.
- Disabled controls use both reduced contrast and native disabled semantics.
- Important controls include concise inline consequences and delayed guidance tooltips.
- Review-page output choices use vertically aligned checkboxes followed by consequence text; RPA creation appears before installation because it changes workspace artifacts, while installation changes the selected game.
- The RPA choice is on by default, and its inline copy always states that validated RPY files remain available when it is off.
- API-contacting actions state whether they normally consume Tokens.
- Keyboard focus follows the visual order; dialogs support `Escape`, and confirmation supports `Return` where safe.
- Text contrast targets WCAG AA. Color is never the only status signal.
- Settings and the single language toggle use the same button factory, vertical padding, and top-bar baseline.

## Prohibited patterns

- Do not instantiate page-specific `tk.Button`, `ttk.Entry`, `ttk.Combobox`, `Listbox`, or default `Scrollbar` controls.
- Do not center form labels, instructions, lists, or arbitrary page sections.
- Do not move the primary workflow action between pages.
- Do not apply `Card.TFrame` to an internal layout row.
- Do not add one-off colors, padding values, fonts, or control heights outside `Colors`, `Metrics`, and `_configure_styles`.
- Do not use decorative emoji as interface icons.
- Do not add a profile-import control to the desktop model flow; built-in presets and automatic non-secret settings persistence are the supported path.
- Do not rebuild the model page for provider selection, connection, or verification state changes; update existing widgets in place.
- Provider grids use equal columns and equal outer/inter-item gutters; no column receives a wider clickable tile because of asymmetric padding.
- Model catalogs must reflect the current API response only. Never merge hard-coded model IDs into a successful live response.
- API keys use encrypted OS credential storage by default and may be changed to memory-only in the shared Settings dialog. They never enter JSON settings, profiles, logs, screenshots, or packages.

## Visual QA checklist

Before merging UI changes, render all five workflow pages plus the model picker and error dialog at default, compact, and minimum window sizes. Verify:

1. Left edges, field heights, button heights, and footer slots do not move between pages.
2. English and Simplified Chinese copy does not clip.
3. Hover, focus, selected, disabled, paused, failed, and unavailable-usage states remain legible.
4. Text fields, list rows, diagnostic text, and scrollbars use the documented styles.
5. No control is hidden behind the fixed footer and no horizontal scrollbar is required.
6. Provider and API action clicks do not replace the current page widget tree or change the current scroll position.
7. Resize transitions preserve the current page widget identity, keyboard focus, and scroll position at every breakpoint.
8. Windows builds render one GUI window with an integrated dark title bar and no console window.
9. CI uploads 36 pixel screenshots covering the bilingual workflow, three responsive sizes, connecting/retry/paused states, Settings, model selection, and the error dialog; inspect the artifact for clipping and font fallback.
