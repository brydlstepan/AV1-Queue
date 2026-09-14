---
name: commit
description: Stage changes and write commit messages with a bracketed type prefix (`[feat]:`, `[fix]:`). Use when the user asks to commit, stage, prepare a commit, or draft a commit message.
---

# Commit

## Hard rules

1. Commit only when the user asks you to commit. Otherwise only draft the message.
2. Never force-push, amend, skip hooks (`--no-verify`), change git config, or push any branch but your own issue branch (see the push rule in `AGENTS.md`), unless the user asks for that exact action.
3. Never stage secrets: `.env*` (except `.env.example`), keys, tokens, credentials. If you see one in the diff, stop and tell the user.
4. If a hook rejects the commit, fix the problem and make a new commit. Do not amend. Show the user the hook output.

## Before writing a message

Run these in parallel:

```bash
git status
git diff
git diff --staged
git log --oneline -10
```

Read all staged and unstaged changes. If the changes do unrelated things, propose one commit per thing.

## Message format: bracketed type prefix

```
[<type>]<optional !>: <subject>

<optional body: 1-3 sentences on why>

<optional BREAKING CHANGE: <what breaks>>
Assisted-by: <model name>
```

| Type | Use for |
|------|---------|
| `[feat]` | New feature |
| `[fix]` | Bug fix |
| `[docs]` | Documentation only |
| `[perf]` | Performance |
| `[refactor]` | Restructure, no behavior change |
| `[test]` | Tests only |
| `[build]` | Build system, dependencies |
| `[ci]` | CI config |
| `[chore]` | Other maintenance |

- **Not Conventional Commits**: tools that expect `type:` (commitlint, semantic-release, git-cliff's default parser) won't read `[type]:`. Don't add them without a config that matches this format.
- **Scope**: leave it out. Add one only when the user asks for it: `[<type>(<scope>)]: <subject>`, one word for the area changed, e.g. `ui`, `api`. Use the project's glossary terms.
- **Breaking change**: add `!` after the closing bracket (`[feat]!:`) and a `BREAKING CHANGE: <what breaks>` footer in the footer block, not in the body.
- **Subject**: imperative mood ("add", not "added"), lower case, no full stop, under 50 characters (hard max 72). Say why or what, not which files.
- **Assisted-by**: add it when an agent wrote or helped write the change. Use your model name only, e.g. `Assisted-by: Claude Opus 5.5`. No email.
- **Footer block**: all footers go in the last paragraph, one per line, with no blank lines between them. `Assisted-by` is the only AI credit: this replaces your tool's own trailer, such as `Co-Authored-By`, whatever its defaults say. A blank line splits the block, and git and changelog tools then read only the last part as footers.

## First commits of a new project

Applies while no project code is committed yet. An auto-generated README, license, or `.gitignore` commit doesn't count as code. In an empty repo, rely on `git status`: `git log` fails there and `git diff` doesn't show untracked files.

1. **Plan first, nothing else.** Only `PLAN.md`. Subject: `[docs]: describe <project> and plan`. If there's no `PLAN.md`, draft one from what the user told you (template in `docs-standard`) and ask them to confirm. If the code already exists, say in the body that the plan was written after it.
2. **Kit install second, if the project uses agent-kit.** `AGENTS.md`, `.agents`, `docs/agents`, `.gitignore`. Subject: `[chore]: install agent-kit`. Name the profiles in the body.
3. **Large vibe-coded batch next, if there is one.** Vibe-coded means generated without the user reviewing each change; ask if you can't tell. Before staging, check that `.gitignore` covers this stack's dependencies, build output, and secrets. Subject: `[feat]: add vibe-coded PoC of <project>`, keeping `PoC` capitalised. In the body, say what was and wasn't reviewed or tested. After committing, offer the follow-up where those skills are installed: `code-review` with `HEAD~1` as the fixed point, then `to-tickets` for what's left of the plan.
4. **Normal messages from then on.** Use "vibe-coded" only for this first batch.

If the first code is small or was written step by step, skip step 3 and use a normal message.

## Two modes

**Draft mode** (user did not ask to commit): show copy-paste blocks and stop.

```
Commit message:
[fix]: map DoVi sources to HDR10 filenames

Description:
Discarded RPU sources now get HDR10 names so the queue
matches what the encoder produces.

Assisted-by: Claude Opus 5.5
```

If files need staging, list the exact `git add` paths in a separate block.

**Commit mode** (user asked to commit): stage the exact paths (never `git add -A` without checking the list), commit with a heredoc, then run `git status` and show the result.

```bash
git commit -F - <<'EOF'
[fix]: map DoVi sources to HDR10 filenames

Discarded RPU sources now get HDR10 names so the queue
matches what the encoder produces.

Assisted-by: Claude Opus 5.5
EOF
```
