# Issue and pull request triage

## Assess a report

1. Confirm repository ownership and search for duplicates. Link the original when closing a duplicate. Route misplaced reports with an explanation and a link to the owning repository.
2. Check versions, reproduction steps, expected/actual behavior, and redacted evidence. Apply `status: needs-info` and ask specific questions when details are missing.
3. Keep the primary classification and add relevant area/impact labels. Apply `status: confirmed` when a maintainer confirms the problem or accepts a request. Apply `status: blocked` only with a linked or described blocker.
4. Assign priority after assessing user impact, scope, and workarounds. Leave priority unset until assessed. Keep at most one priority and one triage-status label; replace obsolete status labels as investigation progresses.
5. Link companion issues and PRs for cross-repository work and apply `cross-repo`. Record compatibility requirements and release order. For example, link an Android offline failure to its Orchestrator fix rather than duplicating the investigation.
6. Close with a resolution and relevant fix/release link. Remove obsolete triage-status labels when closing. Use a milestone only when work is planned for that release; a merged fix may not yet be released.

Use GitHub's native open/closed and draft/ready/merged controls. PRs do not need a triage-status label unless one helps investigation. Do not close reports automatically merely because they are old. Assign `good first issue` only to scoped work with an explanation and support available; assign `help wanted` when contributions are welcome and next steps are clear.

Issue forms set the classification and `status: needs-triage`. Area, priority, and impact labels are maintainer assessments. An area dropdown in a report would not automatically assign a GitHub label; apply those labels during triage. Existing dependency/language labels remain available for automation.

## Managed labels

The managed definitions are in [.github/labels.yml](../.github/labels.yml). Shared labels have the same meanings and colors across the four repositories. Area labels reflect this repository's ownership.

| Label                  | Meaning                                                                 |
| ---------------------- | ----------------------------------------------------------------------- |
| `bug`                  | Something is not working                                                |
| `enhancement`          | New capability or improvement                                           |
| `documentation`        | Documentation correction or improvement                                 |
| `question`             | Usage or implementation question                                        |
| `dependencies`         | Dependency updates                                                      |
| `status: needs-triage` | Awaiting initial maintainer assessment                                  |
| `status: needs-info`   | Waiting for reproduction details or other information                   |
| `status: confirmed`    | Maintainer has confirmed the problem or accepted the request            |
| `status: blocked`      | Cannot proceed; blocker is linked in the issue                          |
| `priority: critical`   | Immediate attention: widespread outage, data loss, or comparable impact |
| `priority: high`       | Major impact with limited or no workaround                              |
| `priority: normal`     | Normal planned work                                                     |
| `priority: low`        | Minor impact or optional improvement                                    |
| `regression`           | Previously working behavior has broken                                  |
| `performance`          | Latency, throughput, resource use, or responsiveness                    |
| `breaking-change`      | Requires consumer changes or migration                                  |
| `cross-repo`           | Coordinated work in multiple ZenStream repositories                     |
| `good first issue`     | Scoped newcomer-friendly work with guidance available                   |
| `help wanted`          | Contributions are welcome; scope and next steps are documented          |
| `duplicate`            | Already tracked elsewhere; link the original                            |
| `invalid`              | Does not describe an actionable issue for this repository               |
| `wontfix`              | Maintainer has decided not to pursue this change                        |
| `accessibility`        | Barrier affecting people with disabilities                              |
| `area: api`            | Changes or reports involving api                                        |
| `area: catalog`        | Changes or reports involving catalog                                    |
| `area: metadata`       | Changes or reports involving metadata                                   |
| `area: playback`       | Changes or reports involving playback                                   |
| `area: offline`        | Changes or reports involving offline                                    |
| `area: jobs`           | Changes or reports involving jobs                                       |
| `area: database`       | Changes or reports involving database                                   |
| `area: auth`           | Changes or reports involving auth                                       |
| `area: dashboard`      | Changes or reports involving dashboard                                  |
| `area: launcher`       | Changes or reports involving launcher                                   |
| `area: deployment`     | Changes or reports involving deployment                                 |

## Synchronize GitHub metadata

Install Python 3 and [GitHub CLI](https://cli.github.com/), authenticate with `gh auth login`, and use an account with permission to manage labels and repository topics. Run from the repository root:

```sh
python .github/scripts/sync_github_metadata.py
python .github/scripts/sync_github_metadata.py --apply
```

The first command previews changes; only `--apply` writes. The script uses the explicit repository in [.github/topics.json](../.github/topics.json), regardless of your current CLI repository. It creates missing labels, updates managed colors/descriptions, and adds missing topics. It preserves unmanaged labels/topics and does not relabel existing issues, rename labels, or delete anything. A failed run can be retried; a successful repeat should show no changes.

`labels.yml` uses JSON syntax, a YAML subset, so the sync command needs only Python's standard library and GitHub CLI. Preserve that syntax when editing it. Topics describe the repository for discovery; they are separate from issue and PR labels.

Before publishing templates on the default branch, apply the label definitions so all template-referenced labels exist. Then preview the issue chooser and PR template on GitHub. Local template files alone do not configure remote labels/topics. This change adds no automatic stale closure or path-based labeling.

## Design references

The guidance adapts focused reproduction prompts from [VS Code](https://github.com/microsoft/vscode/blob/main/.github/ISSUE_TEMPLATE/bug_report.md), validation and compatibility prompts from [Flutter](https://github.com/flutter/flutter/blob/master/.github/PULL_REQUEST_TEMPLATE.md), structured environment reports from [Transformers](https://github.com/huggingface/transformers/blob/main/.github/ISSUE_TEMPLATE/bug-report.yml), and deployment/media diagnostics from [Jellyfin](https://jellyfin.org/docs/general/contributing/issues/). These are reference patterns; their project-specific governance policies do not apply here.
