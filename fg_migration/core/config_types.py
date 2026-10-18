"""Configuration read from .migrate.ini, one class per section. Everything else is fixed here"""
import configparser
from dataclasses import dataclass


@dataclass(frozen=True)
class ForgejoConfig:
    """Connection to the Forgejo server ([forgejo] section)"""
    FORGEJO_URL : str
    FORGEJO_API_URL : str
    FORGEJO_API_TOKEN : str
    # client certificate, for a Forgejo behind a proxy requiring one
    FORGEJO_CLIENT_AUTH_CERT : str | None
    FORGEJO_CLIENT_AUTH_KEY : str | None

    # fixed settings
    # the name Forgejo gives the initial team of an organization, with the Owners role
    FORGEJO_DEFAULT_OWNERS_TEAM_NAME = "Owners"
    API_MAX_PAGE_SIZE = 50
    # seconds to wait for one repository migration, Forgejo continues in the background after
    MIGRATE_TIMEOUT = 3600

    @classmethod
    def from_config(cls, config:configparser.RawConfigParser, section:str="forgejo"):
        """Create the frozen instance from config file"""
        forgejo_website_url = config.get(section, option="forgejo_url").rstrip("/")
        return cls(
            FORGEJO_URL = forgejo_website_url,
            FORGEJO_API_URL = f"{forgejo_website_url}/api/v1",
            FORGEJO_API_TOKEN = config.get(section, option="forgejo_token"),
            FORGEJO_CLIENT_AUTH_CERT = config.get(section, option="forgejo_client_auth_cert",
                                                  fallback=None),
            FORGEJO_CLIENT_AUTH_KEY = config.get(section, option="forgejo_client_auth_key",
                                                 fallback=None),
        )



@dataclass(frozen=True)
class GitLabConfig:
    """Connection to the GitLab server ([gitlab] section)"""
    GITLAB_URL : str
    GITLAB_TOKEN : str
    # client certificate, for a GitLab behind a proxy requiring one
    GITLAB_CLIENT_AUTH_CERT : str | None
    GITLAB_CLIENT_AUTH_KEY : str | None

    # fixed settings
    API_MAX_PAGE_SIZE = 50
    # seconds to wait for a GitLab API response before retrying
    REQUEST_TIMEOUT = 60
    # GitLab's documented maximum group nesting
    MAX_DESCENDANT_GROUP_DEPTH = 20
    MAX_SUB_GROUP_DEPTH = 20
    # data read from GitLab is cached here, so reruns do not fetch it again
    CACHE_DIR = ".cache"
    CACHE_MAX_AGE_HOURS = 24

    @classmethod
    def from_config(cls, config:configparser.RawConfigParser, section:str="gitlab"):
        """Create the frozen instance from config file"""
        return cls(
            GITLAB_URL = config.get(section, option="gitlab_url").rstrip("/"),
            GITLAB_TOKEN = config.get(section, option="gitlab_token"),
            GITLAB_CLIENT_AUTH_CERT = config.get(section, option="gitlab_client_auth_cert",
                                                 fallback=None),
            GITLAB_CLIENT_AUTH_KEY = config.get(section, option="gitlab_client_auth_key",
                                                fallback=None),
        )



@dataclass(frozen=True)
class GitLabMigrationConfig:
    """Fixed settings of the migration from GitLab"""
    # likely GitLab system users are imported, with a warning
    IGNORE_GITLAB_SYSTEM_USERS = False
    IGNORED_GITLAB_SYSTEM_USERS = frozenset({"GitLab-Admin-Bot", "ghost", "support-bot",
                                             "alert-bot", "GitLabDuo"})
