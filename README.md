# Migration to Forgejo (Import from GitLab / GitHub)

Migrates users, organizations, repositories and access rights from GitLab (including gitlab.com) or GitHub
(github.com or GitHub Enterprise Server) to Forgejo.

- **Repositories:**
  - git data (all branches and tags) and LFS objects
  - wiki
  - issues and merge / pull requests, with their comments, reactions and reviews
  - labels, milestones and topics
  - releases with their assets
  - **attachments:** images and files uploaded to issues, merge / pull requests and comments, copied into Forgejo
    with their links rewritten (`--attachments`)
  - archived state
  - access rights: members and teams, mapped from GitLab access levels or GitHub permissions
- **Users:** accounts, emails, SSH keys, GPG keys, and avatars (stored in Forgejo, so Forgejo needs no Gravatar
  access). New users get a temporary password.
- **Organizations:** GitLab groups and GitHub organizations, with their teams, members and roles.

Tested with Forgejo 17.0.

## Setup

```
python3 -m venv migration
source migration/bin/activate
python3 -m pip install -r requirements.txt
```

Create `.migrate.ini` (listed in `.gitignore`):

```ini
[source]
type = gitlab
url = https://gitlab.com
token = <personal access token: api, read_repository>

[target]
url = https://forgejo.example.com
token = <Forgejo admin token with all permissions>
```

For GitHub:

```ini
[source]
type = github
url = https://github.com
token = <personal access token (classic): repo, read:org, user:email>
```

For GitHub Enterprise Server, set `url` to the server, e.g. `https://github.example.com`.

Tokens are enough, no passwords are needed. If the source or the target sits behind a proxy that requires
a TLS client certificate, add `client_auth_cert` / `client_auth_key` (paths to the PEM files) to its section.

Older config files with `[gitlab]` (`gitlab_url`, `gitlab_token`) and `[forgejo]` (`forgejo_url`, `forgejo_token`)
still work.

## Usage

```
./migrate.py --all
```

Or run the steps one at a time, in this order:

| Option | Step |
|---|---|
| `--users` | users, their emails, keys and avatars |
| `--groups` | organizations, with teams and members |
| `--projects` | repositories with issues, wiki etc., then access rights |
| `--membership` | access rights only, for repositories already migrated |
| `--attachments` | copy files linked in issues, merge / pull requests and comments (e.g. pasted images) and fix the links |
| `--refresh-cache` | fetch everything from the source again instead of using the cache |
| `--from-start` | go over all items of a step, instead of continuing where an interrupted run stopped |
| `--notify` | email new users |
| `--debug` | more output |

Every step can be interrupted and rerun at any time: source data is cached as it is fetched (in `.cache/<type>`),
and each step continues at the item where it stopped. Items already in Forgejo are skipped.

`./purge_forgejo.py` wipes data from Forgejo, e.g. to restart a test migration.

## Roles and teams

Each access level becomes a Forgejo team in the organization, and repositories get access through those teams:

| GitLab | GitHub | Forgejo team | Permission |
|---|---|---|---|
| Owner (50) | organization owner | Owners | owner |
| Maintainer (40) | admin, maintain | Maintainers | admin |
| Developer (30) | write | Developers | write |
| Reporter (20) | triage | Reporters | read, write issues |
| Guest (10) | read | Guests | read |

- An existing team of the same name is reused, its members keep their access.
- Empty teams are not created.
- Users not covered by a team are added to the repository as collaborators.
- Access levels not in the table (e.g. GitHub custom repository roles) are skipped with a warning.
- GitHub organization members get the organization's base permission.

The teams and their permissions are defined in [`fg_migration/core/roles.py`](fg_migration/core/roles.py).

## Notes

- **Scope:** only your own repositories and organizations, and their members, are migrated, not the whole
  instance.
- **Emails:** private emails are only visible to admins, so users get their public email if they set one,
  otherwise `username@noemail-git.local`. On GitHub, the token's own user gets their primary email.
- **Rate limits:** gitlab.com and GitHub throttle requests. The tool says when it waits.
- **Attachments:** downloading files of private GitLab projects needs the Maintainer role. GitHub serves files
  of private repositories through short-lived signed links, some of them cannot be downloaded with a token and
  are skipped with a warning. File types Forgejo refuses (`[attachment] ALLOWED_TYPES`) are uploaded zipped,
  files that are still refused are skipped with a warning.
- **Repository names** follow the path (`my-project`), not the display name (`My Project`).
- **GitHub:** collaborators are only listed for repositories where you have push access, bots are skipped.

## Credits

Fork of [extorn/migrate-into-forgejo](https://github.com/extorn/migrate-into-forgejo), simplified and improved with
aggressive caching, so nothing is fetched twice (respecting GitLab and GitHub API rate limits / throttling).
