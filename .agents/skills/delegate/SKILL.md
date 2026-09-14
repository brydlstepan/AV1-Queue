---
name: delegate
description: Hand a well-defined issue to a subagent working in its own git worktree and branch, then check its report. Use when the user picks an issue to delegate, or `list-issues` sends one here.
---

# Delegate

One issue = one branch = one worktree = one subagent. The subagent starts cold and **cannot ask the user anything**, so everything it needs goes in the brief.

## 1. Check it's delegable

The issue needs acceptance criteria, known files, and no open questions (the **Ready to delegate** test in `list-issues`). If any is missing, stop and tell the user it needs its own session instead.

## 2. Make the worktree

Names follow the branch rule in `AGENTS.md`: branch `<id>-<slug>`, worktree `../<repo>-wt/<branch>`. `<id>` is the issue number, or `<feature>-<NN>` for a local ticket.

```bash
git fetch origin
git worktree add --no-track -b <branch> ../<repo>-wt/<branch> origin/<default-branch>
```

Then link the skills, because `.claude/skills` is git-ignored and a new worktree doesn't have it:

```bash
mkdir -p ../<repo>-wt/<branch>/.claude && ln -s ../.agents/skills ../<repo>-wt/<branch>/.claude/skills
```

```powershell
New-Item -ItemType Directory -Force ..\<repo>-wt\<branch>\.claude
New-Item -ItemType Junction -Path ..\<repo>-wt\<branch>\.claude\skills -Target (Resolve-Path ..\<repo>-wt\<branch>\.agents\skills)
```

No remote: branch from the local default branch. If the branch or worktree already exists, stop and ask; someone may be working on it.

Local tracker: set the ticket to `Status: doing` in this checkout. `docs/tickets/` is git-ignored, so the worktree won't have it.

## 3. Write the brief

Fill in every field. Paste the issue text in full (local tickets especially).

```markdown
You are working on issue <id>: <title>.

Work only inside <absolute worktree path>, on branch <branch>. Read AGENTS.md there first.

## Issue
<full issue body and relevant comments>

## Done when
<acceptance criteria, as a checklist>

## Relevant files
<paths, plus anything you already know about them>

## How to work
- Load `implement` (it uses `tdd` and `code-review`).
- You are asked to commit: use the `commit` skill.
- Push this branch (`git push -u origin <branch>`) and open a **draft** PR/MR (command in the tracker doc, body from the `pr` skill). Body ends with `Closes #<id>`.
  No remote: stop after the commit.
- Never touch other branches, merge, or force-push.
- If you hit a question only the user can answer, stop and report it. Don't guess.

## Report back
- Branch and PR/MR link
- What changed (3-5 bullets)
- Checks you ran and their result; checks you didn't run
- Decisions you made, and open questions
```

## 4. Dispatch

Start a subagent with the brief, running in the worktree, in the background if your tool allows it. If your tool has no subagents, give the user the brief to paste into a new session opened in the worktree.

## 5. Check the report

- Compare the report with the actual diff (`git -C <worktree> diff origin/<default-branch>...`) and the PR's checks. Flag anything the report claims that the diff doesn't show.
- Comment on the issue with the PR link. Local tracker: set the ticket to `Status: done` once the user accepts the work.
- A stop with open questions: put them to the user, then send the answers to the same subagent, or move the issue to its own session.
- Tell the user: PR link, what changed, what's unverified. They review and merge. Once merged, offer to run `git worktree remove ../<repo>-wt/<branch>` (ask first).

Done when the user has the PR link and your check of it, or the subagent's open questions.
