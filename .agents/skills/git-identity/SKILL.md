---
name: git-identity
description: Check and fix which email commits use, so the right GitHub account gets the credit. Use when a contributor or commit author is wrong, an unwanted email is in the history, or when setting up git identity on a new machine or clone.
---

# Git identity

GitHub credits a commit by the emails inside it: author, committer, and `Co-authored-by`. Who pushed it and who has access play no part, so an email linked to another account makes that account a contributor.

## 1. Settle the identities

Ask the user which email this repo should use, and which emails or accounts must not appear.

Done when you have the wanted email and the list of unwanted ones.

## 2. Audit

```bash
git config --show-origin user.email
git shortlog -sne --all
git shortlog -sne --committer --all
git log --all --format='%(trailers:key=Co-authored-by,valueonly)'
```

On GitHub, also run these. `gh` fills in `{owner}` and `{repo}`.

```bash
gh api 'repos/{owner}/{repo}/contributors' --jq '.[].login'
gh api 'repos/{owner}/{repo}/collaborators' --jq '.[].login'
```

Report each identity you found and where: the config, local commits, or GitHub. Done when each one is marked wanted or unwanted. If they're all wanted and the config is right, say so and stop.

## 3. Fix the config

Recommend one option, show the exact change, and apply it once the user agrees.

| Option | Use when | How |
|---|---|---|
| Folder | The user keeps personal and work repos in separate folders | `includeIf` in the global config |
| This repo | It's a one-off | `git config user.email <wanted-email>` |

For a folder, add this to the end of `~/.gitconfig`, and put `email = <wanted-email>` under `[user]` in `~/.gitconfig-personal`:

```ini
[includeIf "gitdir/i:~/personal/"]
    path = ~/.gitconfig-personal
```

An `includeIf` silently does nothing unless all of these hold:

- It comes after the global `[user]` section, because the later value wins.
- The folder path ends in `/`.
- On Windows, the path is the real long one, with no short `~1` names.

A `user.email` set in the repo itself beats all of this. Remove it with `git config --unset user.email` if it's wrong.

Done when `git config --show-origin user.email`, run inside the repo, prints the wanted email.

## 4. Repair existing commits

Skip this if the audit found no unwanted identity in commits.

**Only the last commit, not pushed:** `git commit --amend --no-edit --reset-author --allow-empty`. This also resets its date.

**Anything else is a history rewrite.** Tell the user that every rewritten commit gets a new hash and that other clones must be replaced, then get their explicit yes. A force-push needs its own yes.

1. Commit or stash all changes. Create a local branch for each remote branch being kept, since only local branches and tags get rewritten.
2. Back up, outside the repo:

   ```bash
   git bundle create ../<repo>-before-identity-fix.bundle --all
   git bundle verify ../<repo>-before-identity-fix.bundle
   ```

3. Rewrite, keeping dates. Set `FILTER_BRANCH_SQUELCH_WARNING=1` to skip git's warning and pause. The filters run in `sh` on every platform.

   ```bash
   git filter-branch --tag-name-filter cat --env-filter '
   if [ "$GIT_AUTHOR_EMAIL" = "<old-email>" ]; then GIT_AUTHOR_EMAIL="<wanted-email>"; fi
   if [ "$GIT_COMMITTER_EMAIL" = "<old-email>" ]; then GIT_COMMITTER_EMAIL="<wanted-email>"; fi
   ' --msg-filter 'sed "/^Co-authored-by:.*<old-email>/Id"' -- --branches --tags
   ```

4. Check: search the output of this for each unwanted email. It must find none.

   ```bash
   git log --branches --tags --format='%ae%n%ce%n%B'
   ```

5. If the user said yes to a force-push, run `git push --force-with-lease origin <branch>` for each branch, and `git push --force origin --tags` if there are tags.

Done when the check finds none and the user has what they asked for on the remote.

## 5. Remove it from GitHub

Offer this only if the user needs the old commits gone from GitHub. After a force-push, they stay reachable by hash, and the contributor list can lag.

The sure way is to delete the repo and create it again. Before recommending that, list what it would lose: issues, PRs, stars, forks, secrets, settings, and Actions history. The user deletes the repo themselves. Then push the rewritten branches, or ask them to contact GitHub Support if the repo is too much to lose.

Done when this prints only wanted accounts:

```bash
gh api 'repos/{owner}/{repo}/commits' --paginate --jq '.[] | .author.login, .committer.login'
```

GitHub prints `null` for an email that isn't linked to any account. Treat that as fine if the email itself is wanted.
