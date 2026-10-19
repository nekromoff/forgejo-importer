"""GitHub as source of the migration (github.com or GitHub Enterprise Server)"""
import re
import time
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from fg_migration.adapters.destination_forgjo import ForgejoRepositoryRole
from fg_migration.core.canonical_types import (CanonicalGpgKey, CanonicalKey,
                                               CanonicalOrganization,
                                               CanonicalOrganizationMembership,
                                               CanonicalOrganizations, CanonicalRepo,
                                               CanonicalRepoMembership, CanonicalRepoMemberships,
                                               CanonicalSystemUser, CanonicalUser)
from fg_migration.core.config_types import SourceConfig
from fg_migration.core.json_cache import JsonCache
from fg_migration.core.roles import GITHUB_ORG_OWNER, GITHUB_PERMISSION_ROLES
from fg_migration.utils import fg_print

# emails the migration makes up for users without a visible email
PLACEHOLDER_EMAIL_DOMAIN = "noemail-git.local"


class GitHubError(Exception):
    """A GitHub API call failed"""
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class GitHubApi:
    """Minimal GitHub REST API client: paging, and waiting (announced) when rate limited"""

    def __init__(self, source_config: SourceConfig):
        self.source_config = source_config
        self.web_url = source_config.URL
        host = urlsplit(source_config.URL).hostname or "github.com"
        self.host = host
        # github.com has its own API host, GitHub Enterprise Server serves it under /api/v3
        self.api_url = ("https://api.github.com" if host == "github.com"
                        else f"{source_config.URL}/api/v3")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {source_config.TOKEN}",
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"})
        if source_config.CLIENT_AUTH_CERT and source_config.CLIENT_AUTH_KEY:
            self.session.cert = (source_config.CLIENT_AUTH_CERT, source_config.CLIENT_AUTH_KEY)
        self.username: str | None = None

    def connect(self) -> str:
        """check the connection, the login of the token's user

        Raises
        ------
            ConnectionError
                If the API cannot be reached or the token is refused
        """
        try:
            user = self.get("/user")
        except (GitHubError, requests.RequestException) as e:
            fg_print.error(f"Failed to connect to GitHub at {self.api_url}! Check the url and"
                           f" token in .migrate.ini: {e}")
            raise ConnectionError("Unable to connect to GitHub") from e
        self.username = user["login"]
        fg_print.info(f"Connected to GitHub ({self.api_url}) as user {self.username}")
        return self.username

    def request(self, url: str, params: dict | None = None,
                headers: dict | None = None) -> requests.Response:
        """GET, retried on timeouts and server errors, waiting when rate limited"""
        if not url.startswith("http"):
            url = self.api_url + url
        for attempt in range(6):
            try:
                response = self.session.get(url, params=params, headers=headers,
                                            timeout=self.source_config.REQUEST_TIMEOUT)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
                continue
            wait = self._rate_limit_wait(response)
            if wait is not None:
                fg_print.warning(f"GitHub rate limit reached ({urlsplit(url).path}), waiting"
                                 f" {wait}s before retrying...")
                time.sleep(wait)
                continue
            if response.status_code >= 500 and attempt < 5:
                time.sleep(2 ** attempt)
                continue
            return response
        return response

    @staticmethod
    def _rate_limit_wait(response: requests.Response) -> int | None:
        """seconds to wait if the response says the rate limit is reached"""
        if response.status_code not in (403, 429):
            return None
        if "Retry-After" in response.headers:
            return int(response.headers["Retry-After"])
        if response.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            return max(1, reset - int(time.time()) + 1)
        if "secondary rate limit" in response.text.lower():
            return 60
        return None

    def get(self, path: str, **params) -> Any:
        """the JSON answer of a GET"""
        response = self.request(path, params=params or None)
        if not response.ok:
            raise GitHubError(response.status_code, self._message(response))
        return response.json()

    def paginate(self, path: str, items_type: str, **params) -> Iterator[dict]:
        """all items of a list, page by page (following the Link header)"""
        url, query = path, dict(params, per_page=100)
        loaded = 0
        while url:
            response = self.request(url, params=query)
            if not response.ok:
                raise GitHubError(response.status_code, self._message(response))
            page = response.json()
            yield from page
            loaded += len(page)
            url = response.links.get("next", {}).get("url")
            query = None # the next link has them
            if url:
                fg_print.print_color(fg_print.Bcolors.DIM,
                                     f"  ... loaded {loaded} {items_type} so far, loading more...")

    @staticmethod
    def _message(response: requests.Response) -> str:
        try:
            return response.json().get("message", response.text[:200])
        except ValueError:
            return response.text[:200]



class GitHubMigrationSource:
    """Reads the data to migrate from GitHub: the repositories of the token's user (own,
       organizations' and collaborations), the user's organizations, and their members and
       collaborators. Not all of GitHub"""

    source_system: str = "GitHub"
    # number of users about to be listed, when known up front (used for progress output)
    _expected_user_count : int | None = None
    # when set, every fetched item (users, members, organizations) is cached immediately
    item_cache : JsonCache | None = None

    def __init__(self, github_api: GitHubApi, source_config: SourceConfig):
        self.api = github_api
        self.source_config = source_config
        self.access_level_role_map = {level: ForgejoRepositoryRole(role_id)
                                      for level, role_id in GITHUB_PERMISSION_ROLES.items()}

    #
    # general
    #

    def get_source_system_name(self) -> str:
        return self.source_system

    def migration_service(self) -> str:
        """the Forgejo migration service to import repositories with"""
        return "github"

    def upload_link_pattern(self) -> re.Pattern:
        """Links to files uploaded to issues, pull requests and comments, e.g.
             https://github.com/user-attachments/assets/<uuid>
             https://github.com/user-attachments/files/<id>/<filename>
             https://user-images.githubusercontent.com/<user id>/<id>-<uuid>.png
             https://github.com/<owner>/<repo>/assets/<user id>/<uuid>
           the secret is the whole link (it is downloaded as is)"""
        hosts = "|".join(re.escape(h) for h in dict.fromkeys(("github.com", self.api.host)))
        return re.compile(
            r"(?P<secret>https://(?:"
            rf"(?:{hosts})/user-attachments/(?:assets|files/\d+)"
            r"|(?:private-)?user-images\.githubusercontent\.com/\d+"
            rf"|(?:{hosts})/[^/\s()<>\"'\[\]]+/[^/\s()<>\"'\[\]]+/(?:assets|files)/\d+"
            r")/(?P<filename>[^/\s()<>\"'\[\]?#]+)(?:\?[^\s()<>\"'\[\]]*)?)")

    def list_mapped_forgejo_repository_roles(self) -> set[ForgejoRepositoryRole]:
        # roles from roles.py, not the custom ones created for unknown access levels
        return {role for role in self.access_level_role_map.values() if not role.is_custom}

    def get_repository_role(self, source_access_level:str) -> ForgejoRepositoryRole:
        """The ForgejoRepositoryRole of a GitHub permission, a custom (unmapped) one if unknown,
           e.g. for custom repository roles of GitHub Enterprise"""
        role = self.access_level_role_map.get(source_access_level)
        if role is None:
            fg_print.error(f"{self.source_system} permission:Role mapping missing for"
                           f" {source_access_level}")
            role = ForgejoRepositoryRole(id=f"{self.source_system}_Role_{source_access_level}",
                                         is_custom=True)
            self.access_level_role_map[source_access_level] = role
        return role

    #
    # repositories
    #

    def _iter_repositories(self) -> Iterator[dict]:
        return self.api.paginate("/user/repos", "repositories",
                                 affiliation="owner,collaborator,organization_member")

    def list_repositories(self) -> list[CanonicalRepo]:
        repos: list[CanonicalRepo] = []
        fg_print.info(f"Listing your {self.source_system} repositories...")
        try:
            for index, repo in enumerate(self._iter_repositories(), start=1):
                fg_print.progress(index, None, f"Reading repository {repo['full_name']}")
                repos.append(CanonicalRepo(
                    source_system=self.source_system,
                    source_id=repo["id"],
                    is_individual=repo["owner"]["type"] == "User",
                    username=repo["name"],
                    name=repo["name"],
                    owner_name=repo["owner"]["login"],
                    clone_url=repo["clone_url"],
                    is_private=repo["private"],
                    description=repo.get("description") or "",
                    # the token is enough for Forgejo to clone and read the GitHub API
                    auth_username=None,
                    auth_password=None,
                    auth_token=self.source_config.TOKEN,
                    source_type="Repository",
                    is_archived=bool(repo.get("archived"))))
        except GitHubError as e:
            fg_print.error(f"Failed to load all repositories: {e}. Import will need to be run"
                           " again for repositories")
        return repos

    def _collaborators(self, owner: str, name: str) -> list[tuple[str, int, str]]:
        """(login, id, permission) of the users with access to a repository"""
        members = []
        for user in self.api.paginate(f"/repos/{owner}/{name}/collaborators",
                                      f"collaborators of {owner}/{name}", affiliation="all"):
            if user.get("type") != "User":
                continue # bots, e.g. dependabot
            permission = user.get("role_name") or self._permission(user.get("permissions", {}))
            members.append((user["login"], user["id"], permission))
        return members

    @staticmethod
    def _permission(permissions: dict) -> str:
        """the highest permission of a GitHub permissions object"""
        for name, key in (("admin", "admin"), ("maintain", "maintain"), ("write", "push"),
                          ("triage", "triage"), ("read", "pull")):
            if permissions.get(key):
                return name
        return "read"

    def list_repository_accessors(self, repo:CanonicalRepo) -> CanonicalRepoMemberships:
        members: list[CanonicalRepoMembership] = []
        try:
            for login, _user_id, permission in self._collaborators(repo.owner_name, repo.name):
                members.append(CanonicalRepoMembership(username=login, repository=repo,
                                                       hierarchy=None, access_level=permission))
        except GitHubError as e:
            # e.g. 403, listing collaborators needs push access
            fg_print.warning(f"Unable to list the collaborators of {repo.owner_name}/{repo.name}:"
                             f" {e}")
        return CanonicalRepoMemberships(source_system=self.source_system, source_type="Users",
                                        members=members)

    #
    # organizations
    #

    def _iter_organizations(self) -> Iterator[dict]:
        return self.api.paginate("/user/orgs", "organizations")

    def _org_members(self, org_login: str) -> list[tuple[str, int, str]]:
        """(login, id, access level) of the members of an organization"""
        details = self.api.get(f"/orgs/{org_login}")
        # members get the base permission (only visible to owners, read otherwise)
        base_permission = details.get("default_repository_permission") or "read"
        owners = {user["login"] for user in self.api.paginate(
            f"/orgs/{org_login}/members", f"owners of {org_login}", role="admin")}
        members = []
        for user in self.api.paginate(f"/orgs/{org_login}/members", f"members of {org_login}"):
            if user.get("type") != "User":
                continue
            level = GITHUB_ORG_OWNER if user["login"] in owners else base_permission
            members.append((user["login"], user["id"], level))
        return members

    def list_organizations(self) -> CanonicalOrganizations:
        organizations = CanonicalOrganizations(source_type="Organizations", members=[])
        fg_print.info(f"Listing your {self.source_system} organizations...")
        try:
            for index, org in enumerate(self._iter_organizations(), start=1):
                cache_key = f"orgs/{org['id']}"
                canonical_org = self._cache_get(cache_key)
                if canonical_org is not None:
                    fg_print.progress(index, None, f"Organization {org['login']} (cached)")
                    organizations.members.append(canonical_org)
                    continue
                fg_print.progress(index, None,
                                  f"Reading organization {org['login']} and its members")
                try:
                    details = self.api.get(f"/orgs/{org['login']}")
                    members = self._org_members(org["login"])
                except GitHubError as e:
                    fg_print.error(f"Failed to load organization {org['login']}: {e}."
                                   " Skipping its membership capture.")
                    continue
                canonical_org = CanonicalOrganization(
                    source_system=self.source_system,
                    source_type="Organization",
                    username=org["login"],
                    full_name=details.get("name") or org["login"],
                    description=details.get("description") or "",
                    members=[CanonicalUser(username=login) for login, _, _ in members],
                    memberships=[CanonicalOrganizationMembership(org_username=org["login"],
                                                                 username=login,
                                                                 access_level=level)
                                 for login, _, level in members if level != "none"])
                self._cache_put(cache_key, canonical_org)
                organizations.members.append(canonical_org)
        except GitHubError as e:
            fg_print.error(f"Failed to load all organizations: {e}. Import incomplete for"
                           " organizations.")
        return organizations

    #
    # users
    #

    def _cache_get(self, key: str) -> Any:
        """cached item or None"""
        return self.item_cache.value(key) if self.item_cache is not None else None

    def _cache_put(self, key: str, data: Any) -> None:
        if self.item_cache is not None:
            self.item_cache.put(key, data)

    def _iter_user_refs(self) -> Iterator[tuple[int, str]]:
        """(id, login) of the users to migrate: the token's user, members of the
           organizations and collaborators of the repositories"""
        fg_print.info("Collecting users from the members of your GitHub organizations and the"
                      " collaborators of your repositories...")
        member_ids: dict[int, str] = {}
        me = self.api.get("/user")
        member_ids[me["id"]] = me["login"]

        fg_print.info("Listing your GitHub repositories...")
        sources: list[tuple[str, str, Callable[[], list[tuple[str, int, str]]]]] = [
            (f"Repository {repo['full_name']} Collaborators", f"members/repo_{repo['id']}",
             lambda repo=repo: self._collaborators(repo["owner"]["login"], repo["name"]))
            for repo in self._iter_repositories()]
        fg_print.info("Listing your GitHub organizations...")
        sources += [(f"Organization {org['login']} Members", f"members/org_{org['id']}",
                     lambda org=org: self._org_members(org["login"]))
                    for org in self._iter_organizations()]

        total = len(sources)
        for index, (label, cache_key, load) in enumerate(sources, start=1):
            cached = self._cache_get(cache_key)
            if cached is not None:
                fg_print.progress(index, total, f"{label} (cached)")
            else:
                fg_print.progress(index, total, f"Reading {label}"
                                                f" ({len(member_ids)} users found so far)")
                try:
                    cached = [[user_id, login] for login, user_id, _ in load()]
                except GitHubError as e:
                    fg_print.warning(f"Skipping users of {label}: {e}")
                    continue
                self._cache_put(cache_key, cached)
            for user_id, login in cached:
                member_ids.setdefault(user_id, login)
        fg_print.info(f"Found {len(member_ids)} distinct users in your GitHub organizations"
                      " and repositories")
        self._expected_user_count = len(member_ids)
        yield from member_ids.items()

    def list_system_users(self) -> list[CanonicalSystemUser]:
        users: list[CanonicalSystemUser] = []
        fg_print.info(f"Listing {self.source_system} users...")
        try:
            for index, (user_id, login) in enumerate(self._iter_user_refs(), start=1):
                cache_key = f"users/{user_id}"
                cached = self._cache_get(cache_key)
                if cached is not None:
                    fg_print.progress(index, self._expected_user_count, f"User {login} (cached)")
                    users.append(cached)
                    continue
                fg_print.progress(index, self._expected_user_count,
                                  f"Reading user {login} (email, SSH and GPG keys)")
                try:
                    user = self._load_user(login)
                except GitHubError as e:
                    fg_print.error(f"Failed to load GitHub user {login}: {e}")
                    continue
                users.append(user)
                # saved right away, so an interrupted run does not fetch this user again
                self._cache_put(cache_key, user)
        except GitHubError as e:
            fg_print.error(f"Failed to load all users: {e}, import will need to be run again"
                           " for users.")
        return users

    def _load_user(self, login: str) -> CanonicalSystemUser:
        user = self.api.get(f"/users/{login}")
        email = user.get("email")
        if not email and login == self.api.username:
            # the token's user may have only private emails, readable with the user:email scope
            try:
                email = next((e["email"] for e in self.api.get("/user/emails")
                              if e.get("primary")), None)
            except GitHubError:
                email = None
        if email:
            fg_print.info(f"Using email of user {login}")
        else:
            email = f"{login}@{PLACEHOLDER_EMAIL_DOMAIN}"
            fg_print.warning(f"No email available for user {login} (GitHub only shows public"
                             f" emails), using {email}")
        keys = [CanonicalKey(name=f"github-{key['id']}", key=key["key"])
                for key in self.api.paginate(f"/users/{login}/keys", f"keys of {login}")]
        gpg_keys = [CanonicalGpgKey(name=key.get("key_id"),
                                    armored_public_key=key.get("raw_key") or key.get("public_key"),
                                    armored_signature=None)
                    for key in self.api.paginate(f"/users/{login}/gpg_keys",
                                                 f"GPG keys of {login}")]
        return CanonicalSystemUser(source_system=self.source_system, username=login,
                                   full_name=user.get("name") or login, email=email,
                                   avatar_url=user.get("avatar_url"), password=None,
                                   gpg_keys=gpg_keys, keys=keys)

    #
    # files
    #

    def download_avatar(self, avatar_url:str) -> bytes | None:
        """The avatar picture in 512px, None if it cannot be downloaded"""
        parts = urlsplit(avatar_url)
        query = dict(parse_qsl(parts.query))
        query["s"] = "512"
        url = parts._replace(query=urlencode(query)).geturl()
        try:
            response = requests.get(url, timeout=self.source_config.REQUEST_TIMEOUT)
        except requests.RequestException as e:
            fg_print.warning(f"Failed to download avatar {url}: {e}")
            return None
        if response.ok and response.headers.get("Content-Type", "").startswith("image/"):
            return response.content
        if response.status_code != 404:
            fg_print.warning(f"Failed to download avatar {url}: HTTP {response.status_code}")
        return None

    def download_upload(self, repo:CanonicalRepo, secret:str, filename:str) -> bytes | None:
        """A file uploaded to an issue / pull request / comment, the secret is its link.
           None if it cannot be downloaded (e.g. private attachments only shown to browsers)"""
        try:
            response = self.api.request(secret, headers={"Accept": "*/*"})
        except requests.RequestException as e:
            fg_print.debug(f"Failed to download {secret}: {e}")
            return None
        # a private attachment answers with the sign in page instead of the file
        if response.ok and "text/html" not in response.headers.get("Content-Type", ""):
            return response.content
        fg_print.debug(f"Failed to download {secret} of {repo.name} ({filename}):"
                       f" HTTP {response.status_code}")
        return None
