---
name: tracker
description: Set up or switch this project's issue tracker (GitHub, GitLab, or local markdown). Use when `docs/agents/issue-tracker.md` is missing, or the user wants to change where specs and tickets go.
---

# Tracker setup

Specs and tickets go to one tracker per project, recorded in `docs/agents/issue-tracker.md`. Skills like `to-spec`, `to-tickets`, `implement` and `code-review` read that file.

## Steps

1. Run `git remote -v`.
   - Remote on `github.com` → propose **github**.
   - Remote on GitLab (gitlab.com or self-hosted) → propose **gitlab**.
   - No remote → propose **local**.
2. Ask the user to confirm. One question, recommended answer first.
3. For github: run `gh auth status`. If not logged in, tell the user to run `gh auth login` with only the `repo` scope (or a fine-grained token limited to this repo). Do not run the login for them.
   For gitlab: same with `glab auth status` / `glab auth login`.
4. Copy the matching file from this skill folder to `docs/agents/issue-tracker.md`:
   - [github.md](./github.md)
   - [gitlab.md](./gitlab.md)
   - [local.md](./local.md)
5. Set `tracker:` in the `## Project` section of `AGENTS.md` and `"tracker"` in `.agents/agent-kit.json` to the same value. The next kit update restores whatever the JSON file says.
6. For local: make sure `.gitignore` has `docs/specs/` and `docs/tickets/`.

Done when `gh repo view` / `glab repo view` prints this project from inside the folder, or (local) the folders are ignored.
