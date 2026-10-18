# Migration to Forgejo (Import from Gitlab)

Migrates users, groups, projects and access rights from GitLab (including gitlab.com) to Forgejo.

- **Repositories:**
  - git data (all branches and tags) and LFS objects
  - wiki
  - issues and merge requests, with their comments, reactions and reviews
  - labels, milestones and topics
  - releases with their assets
  - **attachments:** images and files uploaded to issues, merge requests and comments, copied into Forgejo
    with their links rewritten (`--attachments`)
  - archived state
  - access rights: members and teams, mapped from GitLab access levels
- **Users:** accounts, emails, SSH keys, GPG keys, and avatars of newly created users. New users get a temporary password.
- **Groups:** become Forgejo organizations, with their teams, members and roles mapped from GitLab access levels.

Tested with gitlab.com (GitLab 19.6) and Forgejo 17.0.

## Setup

```
python3 -m venv migration
source migration/bin/activate
python3 -m pip install -r requirements.txt
```

Create `.migrate.ini` (listed in `.gitignore`):

```ini
[forgejo]
forgejo_url = https://forgejo.example.com
forgejo_token = <admin token with all permissions>

[gitlab]
gitlab_url = https://gitlab.com
gitlab_token = <personal access token: api, read_repository>
```

Tokens are enough, no GitLab or Forgejo passwords are needed. Only if GitLab or Forgejo sits behind a proxy
that requires a TLS client certificate, add `gitlab_client_auth_cert` / `gitlab_client_auth_key` to `[gitlab]`,
or `forgejo_client_auth_cert` / `forgejo_client_auth_key` to `[forgejo]` (paths to the PEM files).

## Usage

```
./migrate.py --all
```

Or run the steps one at a time, in this order:

| Option | Step |
|---|---|
| `--users` | users, their emails, keys and avatars |
| `--groups` | groups as organizations, with teams and members |
| `--projects` | repositories with issues, wiki etc., then access rights |
| `--membership` | access rights only, for repositories already migrated |
| `--attachments` | copy files linked in issues, merge requests and comments (e.g. pasted images) and fix the links |
| `--refresh-cache` | fetch everything from GitLab again instead of using the cache |
| `--from-start` | go over all items of a step, instead of continuing where an interrupted run stopped |
| `--notify` | email new users |
| `--debug` | more output |

Every step can be interrupted and rerun at any time: GitLab data is cached as it is fetched, and each step
continues at the item where it stopped. Items already in Forgejo are skipped.

`./purge_forgejo.py` wipes data from Forgejo, e.g. to restart a test migration.

## Roles and teams

Each GitLab access level becomes a Forgejo team in the organization, and repositories get access through those teams:

| GitLab | Forgejo team | Permission |
|---|---|---|
| Owner (50) | Owners | owner |
| Maintainer (40) | Maintainers | admin |
| Developer (30) | Developers | write |
| Reporter (20) | Reporters | read, write issues |
| Guest (10) | Guests | read |

- An existing team of the same name is reused, its members keep their access.
- Empty teams are not created.
- Users not covered by a team are added to the repository as collaborators.
- Access levels not in the table are skipped with a warning.

The teams and their permissions are defined in [`fg_migration/core/roles.py`](fg_migration/core/roles.py).

## Notes

- **gitlab.com:** only your own projects and groups, and their members, are migrated, not the whole instance.
  Private emails are only visible to GitLab admins, so users get their public email if they set one,
  otherwise `username@noemail-git.local`.
- **Rate limits:** gitlab.com throttles requests. The tool says when it waits.
- **Attachments:** downloading files of private GitLab projects needs the Maintainer role. File types Forgejo
  refuses (`[attachment] ALLOWED_TYPES`) are uploaded zipped, files that are still refused are skipped with a warning.
- **Repository names** follow the GitLab path (`my-project`), not the display name (`My Project`).

## Credits

Fork of [extorn/migrate-into-forgejo](https://github.com/extorn/migrate-into-forgejo), simplified and improved with aggresive caching, so nothing is synced multiple times (respecting Gitlab API rate limits / throttling).
