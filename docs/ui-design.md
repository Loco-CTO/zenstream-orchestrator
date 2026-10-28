# Orchestrator interface design

## Design goal

Help administrators understand current server state, find the relevant setting or maintenance action, and recover from problems without learning internal implementation details. New UI must look and behave like the existing surface it belongs to: the browser dashboard and Windows launcher each have an established design.

## Start from the existing design

Choose the nearest implemented screen with the same task before changing UI. Use its components, geometry, and interaction states as the comparison throughout the change.

| Concern                                    | Existing reference                                                                                                                     |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| Dashboard colors, typography, and surfaces | [Dashboard global styles](../frontend/app/globals.css)                                                                                 |
| Dashboard navigation and responsive drawer | [Admin shell](../frontend/app/web/dashboard/components/admin-shell.tsx)                                                                |
| Dashboard headers, cards, and dialogs      | [Shared dashboard surfaces](../frontend/app/web/dashboard/components/dashboard-surface.tsx)                                            |
| Configuration forms and item actions       | [Libraries](../frontend/app/web/dashboard/libraries/page.tsx) and [calendar settings](../frontend/app/web/dashboard/calendar/page.tsx) |
| Job overview and detail                    | [Tasks](../frontend/app/web/dashboard/jobs/page.tsx) and [task detail](../frontend/app/web/dashboard/jobs/detail/page.tsx)             |
| Windows launcher layout and tokens         | [Launcher app](../launcher/src/renderer/App.tsx) and [launcher styles](../launcher/src/renderer/styles.css)                            |

Reuse or extend the owning shared component before creating another variant. Match the actual icon imports, sizing, and state treatment in that surface's components. Do not introduce a new icon dependency or transplant the Web client's or Android client's styling. Existing markup is a visual reference; accessibility and interaction defects still need correction in the changed area.

## Navigation and task flow

- Keep dashboard destinations, sidebar order, labels, and active states consistent with the admin shell. The responsive drawer must expose the same destinations and close predictably after selection.
- Place item-specific operations beside the affected library, job, provider, or account. Avoid duplicate global navigation or a second settings hierarchy for a single feature.
- Use a page for a destination or substantial configuration task, tabs for peer views, and a dialog for a short bounded action. Avoid nested dialogs and long forms hidden in small popups.
- Preserve the static `/web/*` routes, query-string detail links, browser Back/Forward, and normal link behavior. Make the return from detail to its overview clear and retain relevant filtering or selection.
- Label actions by their effect, such as "Scan library", "Save settings", or "Remove library". Distinguish removing a configuration/catalog entry from deleting media wherever the distinction affects a decision.
- Keep the Windows launcher's Configuration and Logs sections and the existing lifecycle controls recognizable. The launcher supervises the process; catalog/account management belongs to the dashboard.

## Layout and information density

- Lead with the page title, relevant current state, and dominant next action. Follow with settings or records, then optional diagnostic detail.
- Match the nearest page's width, padding, spacing, row/table density, card geometry, and alignment. Reuse shared headers and surfaces instead of adding decorative card layers.
- Give each task group a clear heading and a coherent save/action model. Place help beside the field it explains; include units, scope, and restart requirements when they affect the result.
- Show routine configuration first. Put advanced options, raw identifiers, logs, payloads, and long error details behind labeled disclosure or an existing detail view.
- Keep essential warnings, active work, and consequential choices visible. Avoid repeating one state in a card, badge, chart, and paragraph.
- Tables should prioritize identification, state, and the main action. Put secondary fields in detail views when needed; preserve labels and action access at narrow widths.

## Visual language

- The dashboard uses its existing dark neutral surfaces, teal accent roles, Inter text, and DM Mono diagnostic text from `frontend/app/globals.css`. Reuse the existing token roles; a new feature should not get its own palette, font scale, or shadow system.
- Preserve production route geometry, shared dialog styling, and the owning component's icon family. Match primary, secondary, selected, warning, and danger treatments by their role.
- The launcher uses `launcher/src/renderer/styles.css`, including its own surface and accent tokens. Keep changes consistent with adjacent launcher controls; do not apply dashboard CSS tokens to it.
- Use concise labels and familiar terms. Technical names belong where an administrator needs them to configure or diagnose something, with a plain-language explanation when necessary.
- Use restrained transitions that clarify changes. Avoid animated metrics, moving controls, or emphasis that competes with a system warning or main action.

## Interaction and feedback

- Reflect the actual lifecycle/job state. Distinguish queued, running, completed, failed, and saved-but-restart-required states; show progress only when the service provides meaningful progress.
- Give immediate pending feedback and prevent unsafe duplicate submissions. Keep safe navigation and diagnostic access available during long work.
- Retain form values after failure. Show field validation near the field and an actionable error for the task; keep useful existing data visible during refresh.
- State whether changes save immediately or require Save. Warn about genuine unsaved data loss and consequential destructive actions, with the affected item and clear Cancel.
- Do not make a transient toast the only record of a failed long task. Offer the existing task/detail/log route for supporting information.
- Dialogs require a title, an obvious exit, appropriate initial focus, contained keyboard focus, and focus restoration on close. Edited values must not disappear through accidental backdrop dismissal.

## Platform behavior and accessibility

### Browser dashboard

- Meet WCAG 2.2 AA for changed UI. Use semantic headings, buttons, links, table headers, associated form labels, and accessible state announcements.
- Support keyboard and touch, visible focus, and accessible icon names. Keep repeated navigation in a consistent relative order.
- Targets must meet the 24 by 24 CSS pixel minimum or an applicable WCAG exception; prefer larger, well-spaced targets for frequent touch actions.
- Check narrow windows, zoom, long labels, and dialogs with validation errors. Keep focused elements and actions clear of sticky chrome and within the usable viewport.
- Maintain contrast for muted text, field boundaries, focus rings, and status on dark surfaces. Pair status colors with text or semantic state.

### Windows launcher

- Preserve the existing window/tray behavior and host bridge for file/directory pickers. Do not reinterpret a window action as stopping the server.
- Keep Start, Stop, Restart, Open dashboard, and configuration save outcomes distinct and consistent with the current lifecycle state.
- Support keyboard traversal, visible focus, accessible control names, Windows text/display scaling, and a usable resized window. Keep critical status and recovery controls reachable while logs or configuration scroll.
- The Electron renderer still needs semantic HTML and screen-reader checks; its desktop window does not automatically supply accessible behavior to custom controls.

## Review checklist

Before calling a UI change complete, review the affected flow and record which checks were actually performed:

- [ ] Name the existing dashboard page or launcher component used as the reference and compare at the same window size.
- [ ] An administrator can locate the task, identify the affected resource/state, and find the next action without reading implementation notes.
- [ ] Navigation, browser history or launcher sections, dialog dismissal, and return state behave predictably.
- [ ] Typography, spacing, colors, icons, and control states match the owning surface.
- [ ] Loading, empty, validation, pending, success, failure, and restart-required states are understandable where applicable.
- [ ] Keyboard/focus, assistive access, zoom/scaling, narrow layouts, and long content have been reviewed.

A successful frontend or launcher build alone does not establish visual or navigation acceptance. State any visual, browser, or Windows interaction checks that remain unverified.

## Design references

- [Nielsen Norman Group: usability heuristics](https://www.nngroup.com/articles/ten-usability-heuristics/)
- [W3C: consistent navigation](https://www.w3.org/WAI/WCAG22/Understanding/consistent-navigation.html)
- [W3C: Web Content Accessibility Guidelines 2.2](https://www.w3.org/TR/WCAG22/)
- [W3C: target size minimum](https://www.w3.org/WAI/WCAG22/Understanding/target-size-minimum.html)
- [W3C: modal dialog interaction](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)
- [Microsoft: Windows accessibility](https://learn.microsoft.com/en-us/windows/apps/design/accessibility/accessibility-overview)
