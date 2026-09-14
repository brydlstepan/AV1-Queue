# Issue tracker: local markdown

Specs and tickets for this project live as markdown files under `docs/specs/` and `docs/tickets/`. Both folders are git-ignored: they exist only on this machine.

## Conventions

- **Spec**: `docs/specs/<feature-slug>.md`.
- **Tickets**: one file per ticket at `docs/tickets/<feature-slug>/<NN>-<slug>.md`, numbered from `01` in dependency order. Never one combined file.
- **Header** at the top of each ticket:

  ```
  Status: todo        # todo | doing | done
  Labels: ready-for-agent
  Blocked by: 01, 02  # or: none
  Spec: docs/specs/<feature-slug>.md
  ```

- **Comments**: append under a `## Comments` heading at the bottom of the file.
- A ticket is unblocked when every ticket in its `Blocked by` line has `Status: done`.
- **Status changes**: set `doing` when work starts and `done` when the user accepts the work. Edit the file in the main checkout, because a worktree doesn't have this git-ignored folder.

## When a skill says "publish to the issue tracker"

Create the file under `docs/specs/` or `docs/tickets/<feature-slug>/`, creating folders as needed.

## When a skill says "fetch the relevant ticket"

Read the file at the path the user gives, or find it by number under `docs/tickets/`.

## Data-loss warning

These files are not in git. If the user has done a lot of work here, suggest moving the project to GitHub or GitLab, or un-ignoring `docs/specs/` and `docs/tickets/`.
