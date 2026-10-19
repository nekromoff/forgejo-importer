"""Configuration read from .migrate.ini: [source] (GitLab or GitHub) and [target] (Forgejo).
   Everything else is fixed here"""
import configparser
from dataclasses import dataclass

from fg_migration.utils import fg_print

SOURCE_TYPES = ("gitlab", "github")


def _section(config: configparser.RawConfigParser, section: str, legacy: str) -> str:
    """the section to read, the legacy name ([gitlab] / [forgejo]) of older config files works too"""
    if config.has_section(section) or not config.has_section(legacy):
        return section
    fg_print.info(f"Reading [{legacy}] of .migrate.ini as [{section}], see the README for the"
                  " current format")
    return legacy


def _get(config: configparser.RawConfigParser, section: str, option: str, legacy_option: str,
         **kwargs) -> str | None:
    """an option, or its legacy name (e.g. gitlab_url for url)"""
    if config.has_option(section, option):
        return config.get(section, option)
    return config.get(section, legacy_option, **kwargs)


@dataclass(frozen=True)
class ForgejoConfig:
    """Connection to the target Forgejo server ([target] section)"""
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
    def from_config(cls, config:configparser.RawConfigParser):
        """Create the frozen instance from config file"""
        section = _section(config, "target", "forgejo")
        url = _get(config, section, "url", "forgejo_url").rstrip("/")
        return cls(
            FORGEJO_URL = url,
            FORGEJO_API_URL = f"{url}/api/v1",
            FORGEJO_API_TOKEN = _get(config, section, "token", "forgejo_token"),
            FORGEJO_CLIENT_AUTH_CERT = _get(config, section, "client_auth_cert",
                                            "forgejo_client_auth_cert", fallback=None),
            FORGEJO_CLIENT_AUTH_KEY = _get(config, section, "client_auth_key",
                                           "forgejo_client_auth_key", fallback=None),
        )



@dataclass(frozen=True)
class SourceConfig:
    """Connection to the source server ([source] section): GitLab or GitHub"""
    TYPE : str # gitlab or github
    URL : str # web url, e.g. https://gitlab.com or https://github.com
    TOKEN : str
    # client certificate, for a source behind a proxy requiring one
    CLIENT_AUTH_CERT : str | None
    CLIENT_AUTH_KEY : str | None

    # fixed settings
    API_MAX_PAGE_SIZE = 50
    # seconds to wait for an API response before retrying
    REQUEST_TIMEOUT = 60
    # GitLab's documented maximum group nesting
    MAX_DESCENDANT_GROUP_DEPTH = 20
    MAX_SUB_GROUP_DEPTH = 20
    # data read from the source is cached here, so reruns do not fetch it again
    CACHE_DIR = ".cache"
    CACHE_MAX_AGE_HOURS = 24

    @classmethod
    def from_config(cls, config:configparser.RawConfigParser):
        """Create the frozen instance from config file"""
        section = _section(config, "source", "gitlab")
        source_type = (config.get(section, "type", fallback=None)
                       or ("gitlab" if section == "gitlab" else "")).strip().lower()
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"[source] type must be one of {', '.join(SOURCE_TYPES)},"
                             f" not '{source_type}'")
        return cls(
            TYPE = source_type,
            URL = _get(config, section, "url", "gitlab_url").rstrip("/"),
            TOKEN = _get(config, section, "token", "gitlab_token"),
            CLIENT_AUTH_CERT = _get(config, section, "client_auth_cert",
                                    "gitlab_client_auth_cert", fallback=None),
            CLIENT_AUTH_KEY = _get(config, section, "client_auth_key",
                                   "gitlab_client_auth_key", fallback=None),
        )



@dataclass(frozen=True)
class GitLabMigrationConfig:
    """Fixed settings of the migration from GitLab"""
    # likely GitLab system users are imported, with a warning
    IGNORE_GITLAB_SYSTEM_USERS = False
    IGNORED_GITLAB_SYSTEM_USERS = frozenset({"GitLab-Admin-Bot", "ghost", "support-bot",
                                             "alert-bot", "GitLabDuo"})
