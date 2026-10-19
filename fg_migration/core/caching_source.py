"""The source (GitLab or GitHub) as used by the migration: its data, cached in JSON files"""
import configparser
import hashlib
import os
import re
from typing import Any, Callable

from fg_migration.core.canonical_types import (CanonicalOrganizations, CanonicalRepo,
                                               CanonicalRepoMemberships, CanonicalSystemUser)
from fg_migration.core.json_cache import JsonCache
from fg_migration.core.config_types import GitLabMigrationConfig, SourceConfig
from fg_migration.adapters.destination_forgjo import ForgejoRepositoryRole
from fg_migration.adapters.gitlab_types import GitLabApiBuilder
from fg_migration.adapters.source_gitlab import GitLabMigrationSource
from fg_migration.adapters.source_github import GitHubApi, GitHubMigrationSource
from fg_migration.utils import fg_print


def build_source(config: configparser.RawConfigParser,
                 refresh_cache: bool = False) -> "CachedSource":
    """Connect to the source configured in [source] and build the cached source

    Raises
    ------
        ConnectionError
            If a quick diagnostic test of the source API fails
    """
    source_config = SourceConfig.from_config(config=config)
    if source_config.TYPE == "github":
        github_api = GitHubApi(source_config)
        username = github_api.connect()
        source = GitHubMigrationSource(github_api=github_api, source_config=source_config)
    else:
        gl_api_builder = GitLabApiBuilder(source_config)
        gl_api = gl_api_builder.build_gitlab_api_client()
        if not gl_api_builder.test_gitlab_connection(gl_api):
            raise ConnectionError("Unable to connect to GitLab")
        username = gl_api.user.username
        source = GitLabMigrationSource(gitlab_api=gl_api, source_config=source_config,
                                       gitlab_migration_config=GitLabMigrationConfig())

    # a cache is only used for the source url and user it was written for
    identity = f"{source_config.TYPE}|{source_config.URL}|{username}"
    # credentials are never written to the cache, they are restored from the config
    secrets = {"auth_password": None, "auth_username": None, "auth_token": source_config.TOKEN}
    cache_dir = os.path.join(SourceConfig.CACHE_DIR, source_config.TYPE)
    fg_print.info(f"Caching {source.get_source_system_name()} data in {cache_dir}"
                  + (" (refreshing it)" if refresh_cache else ""))
    # items (users, members, groups) are cached as soon as they are fetched,
    # so an interrupted run resumes where it stopped
    source.item_cache = JsonCache(cache_dir=os.path.join(cache_dir, "items"),
                                  max_age_hours=SourceConfig.CACHE_MAX_AGE_HOURS,
                                  identity=identity, secrets=secrets, refresh=refresh_cache)
    # complete listings, so a rerun does not fetch anything again
    return CachedSource(source=source,
                        cache=JsonCache(cache_dir=cache_dir,
                                        max_age_hours=SourceConfig.CACHE_MAX_AGE_HOURS,
                                        identity=identity, secrets=secrets,
                                        refresh=refresh_cache))



class CachedSource:
    """Caches the complete results of the listing calls of the source (GitLab or GitHub),
       so a rerun does not need to fetch everything again.
       (The source itself caches each item as it is fetched, so an interrupted
       listing resumes where it stopped, see JsonCache)"""

    def __init__(self, source: GitLabMigrationSource | GitHubMigrationSource, cache: JsonCache):
        self.source = source
        self.cache = cache

    #
    # cached listings
    #

    def list_repositories(self) -> list[CanonicalRepo]:
        return self._cached("projects", "repositories", self.source.list_repositories)

    def list_organizations(self) -> CanonicalOrganizations:
        return self._cached("organizations", "organizations", self.source.list_organizations)

    def list_system_users(self) -> list[CanonicalSystemUser]:
        return self._cached("users", "users", self.source.list_system_users)

    def list_repository_accessors(self, repo: CanonicalRepo) -> CanonicalRepoMemberships:
        return self._cached(os.path.join("repo_accessors", str(repo.source_id)),
                            f"members of {repo.name}",
                            lambda: self.source.list_repository_accessors(repo))

    #
    # not cached, cheap or local
    #

    def get_source_system_name(self) -> str:
        return self.source.get_source_system_name()

    def migration_service(self) -> str:
        return self.source.migration_service()

    def upload_link_pattern(self) -> re.Pattern:
        return self.source.upload_link_pattern()

    def get_repository_role(self, source_access_level: str) -> ForgejoRepositoryRole:
        return self.source.get_repository_role(source_access_level)

    def list_mapped_forgejo_repository_roles(self) -> set[ForgejoRepositoryRole]:
        return self.source.list_mapped_forgejo_repository_roles()

    def download_avatar(self, avatar_url: str) -> bytes | None:
        """the avatar picture (cached, also when there is none)"""
        name = hashlib.sha256(avatar_url.encode()).hexdigest()
        path = os.path.join(self.cache.cache_dir, "avatars", name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        if os.path.exists(f"{path}.none"):
            return None

        data = self.source.download_avatar(avatar_url)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if data is None:
                open(f"{path}.none", "wb").close()
            else:
                with open(f"{path}.tmp", "wb") as f:
                    f.write(data)
                os.replace(f"{path}.tmp", path)
        except OSError as e:
            fg_print.warning(f"Unable to write cache file {path}: {e}")
        return data

    def download_upload(self, repo: CanonicalRepo, secret: str, filename: str) -> bytes | None:
        # an upload never changes (its secret is part of the link), so the cache never expires
        safe_name = os.path.basename(filename) or "file"
        path = os.path.join(self.cache.cache_dir, "uploads", str(repo.source_id), secret, safe_name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()

        data = self.source.download_upload(repo, secret, filename)
        if data is not None:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(f"{path}.tmp", "wb") as f:
                    f.write(data)
                os.replace(f"{path}.tmp", path)
            except OSError as e:
                fg_print.warning(f"Unable to write cache file {path}: {e}")
        return data

    def _cached(self, key: str, description: str, load: Callable[[], Any]) -> Any:
        hit = self.cache.get(key)
        if hit is not None:
            data, age = hit
            fg_print.info(f"Using cached {self.source.get_source_system_name()} {description}"
                          f" from {self.cache.path(key)} (saved {fg_print.format_duration(age)}"
                          " ago, run with --refresh-cache to reload)")
            return data

        result = load()
        self.cache.put(key, result)
        return result
