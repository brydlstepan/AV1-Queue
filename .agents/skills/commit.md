# Git workflow & commits (AV1 Queue)

**On-demand skill** — load only when the user asks to commit, stage, or draft a commit message.

AV1 Queue is a local Windows AV1 encode studio: FastAPI + Uvicorn (`server/`), queue/encode core (`core/`), vanilla UI (`server/static/`), tray launcher (`scripts/`).

## When to use

- User asks to commit, stage, or draft a commit message
- User asks what should go in a commit for current changes
- User says "prepare commit" or needs message/description text for a Git GUI

## Hard rules

1. **Only commit when the user explicitly asks** — otherwise prepare the message only
2. When the user **does** ask to commit: stage relevant paths, commit via HEREDOC, then `git status` (follow the user’s committing-changes rule)
3. When they only want a draft / GUI paste: present copy-paste blocks — do **not** run `git commit`
4. **Never** update git config, force-push, skip hooks, or amend unless user rules allow
5. **Never push** unless explicitly asked

## Before drafting a message

Run in parallel:

```bash
git status
git diff
git diff --staged
git log --oneline -10
```

Review **all** staged and unstaged changes. Split unrelated work into separate commits when the user wants commits (ask if unclear).

## Branch context

Day-to-day work is on **`main`** (this is pre-alpha). Do not invent a `develop` / `feature/*` release flow unless the user says otherwise. No force-push to `main`.

## Commit message format

Use bracketed prefixes `[Prefix]:` — going forward (older history may be plain prose; new commits use this style):

| Prefix | Use for |
|--------|---------|
| `[feat]:` | New feature |
| `[fix]:` | Bug fix |
| `[docs]:` | Documentation only |
| `[perf]:` | Performance / optimization |
| `[refactor]:` | Restructure, no behavior change |
| `[chore]:` | Maintenance, deps, config, tooling |

**Subject line:** under 50 characters when practical (hard max 72). Focus on **why**, not a file list.

**Body (optional):** 1–3 sentences on motivation and notable impact. Use a HEREDOC body when committing from the shell.

**AI signature (required when drafting):** last line of the description / commit body:

```
Co-authored-by: (ModelName)
```

Use the model name only — no app name or email. Example: `Co-authored-by: (Composer)`

## Output format when preparing (no commit yet)

Present **two copy-paste blocks** for Git GUI users:

```
Commit message:
[fix]: name DoVi outputs as HDR10

Description:
Name outputs from encode policy (HDR10 / HDR10plus) at
queue-add time so the inspector matches what SVT emits.

Co-authored-by: (Composer)
```

If staging is needed and the user asked to commit, list exact `git add` paths — separate from the message blocks.

## Examples (AV1 Queue–shaped)

**UI:**
```
Commit message:
[feat]: merge gauges into Pipeline sidebar

Description:
Keep CPU/GPU/RAM under pipeline specs and drop the
separate Performance section so the sidebar stays one column.

Co-authored-by: (Composer)
```

**Encode / core:**
```
Commit message:
[fix]: map DoVi sources to HDR10 filenames

Description:
hdr_label maps discarded RPU sources to HDR10 (or HDR10plus
when that layer is kept) so queue names match encode policy.

Co-authored-by: (Composer)
```

**Docs / chore:**
```
Commit message:
[chore]: add project commit skill

Description:
On-demand commit guidance for AV1 Queue and the
[prefix] + Co-authored-by message format.

Co-authored-by: (Composer)
```

## If commit fails (hook rejected)

- Do **not** amend — fix the issue and create a **new** commit
- Report hook output to the user
