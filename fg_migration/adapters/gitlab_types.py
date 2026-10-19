"""A series of classes involved in the interaction with the GitLab API"""
import os
import time
from typing import Callable, Iterator, TypeVar

import gitlab  # pip install python-gitlab
import gitlab.v4 # pylint: disable=no-name-in-module
import gitlab.v4.objects
import requests

from fg_migration.utils import fg_print
from fg_migration.core.config_types import SourceConfig


class IterativeFetchError(Exception):
    """Raised when the ApiPaginator fails to retrieve the next page of data for some reason"""

T = TypeVar("T")


class GitLabApiPaginator:
    """Wraps the GitLab API with pagination support where errors are logged"""
    gl_api:gitlab.Gitlab
    max_page_size:int
    items_type:str
    retrieval_detail:str

    def __init__(self, gl_api:gitlab.Gitlab, page_size:int=50,
                 items_type:str="Items", retrieval_detail:str=""):
        self.gl_api = gl_api
        self.max_page_size = page_size
        self.items_type = items_type
        self.retrieval_detail = retrieval_detail

    def iterate(self, fetch_page_from_api: Callable[[gitlab.Gitlab, int, int], list[T]],
        ) -> Iterator[T]:
        """Create the API pagination wrapped in an Iterator"""

        page_idx = 1
        try:
            while True:
                page_of_data : list
                for attempt in range(3):
                    try:
                        page_of_data = fetch_page_from_api(self.gl_api,page_idx, self.max_page_size)
                        break
                    except TimeoutError:
                        if attempt == 2:
                            raise
                        time.sleep(2 ** attempt)
                yield from page_of_data
                if len(page_of_data) < self.max_page_size:
                    # no more to load
                    break
                # long lists take a while, so report as each further page is requested
                fg_print.print_color(fg_print.Bcolors.DIM,
                                     f"  ... loaded {page_idx * self.max_page_size} "
                                     f"{self.items_type} so far, loading more...")
                page_idx += 1
        except Exception as e:
            msg = f"Failed to retrieve existing {self.items_type} page[{page_idx}]" \
                  f"{self.retrieval_detail} {e}"
            fg_print.error(msg)
            raise IterativeFetchError(msg) from e


class GitLabApiBuilder:
    """A builder for the GitLab, configuring authentication etc in a central way"""
    config : SourceConfig

    def __init__(self, source_config:SourceConfig):
        self.config = source_config

    def build_gitlab_api_client(self) -> gitlab.Gitlab:
        """Build a GitLab API Client using either the API key provided, or the default API key"""
        session = requests.Session()
        # add client authentication if cert and key are provided in the config
        if(self.config.CLIENT_AUTH_CERT is not None
           and self.config.CLIENT_AUTH_KEY is not None):
            cert_path = self.config.CLIENT_AUTH_CERT
            key_path = self.config.CLIENT_AUTH_KEY
            session.cert = (cert_path, key_path)
        # python-gitlab waits silently when rate limited, so say why nothing happens
        session.hooks["response"].append(self._report_rate_limit)
        # private token or personal token authentication
        gl = gitlab.Gitlab(url = self.config.URL,
                           private_token=self.config.TOKEN,
                           session=session,
                           # without a timeout a stalled connection hangs forever
                           timeout=self.config.REQUEST_TIMEOUT,
                           retry_transient_errors=True)
        try:
            gl.auth()
        except gitlab.GitlabAuthenticationError:
            fg_print.error("Failed to authenticate with GitLab! Check "
                           "access token and client authentication settings in .migrate.ini")
            os.sys.exit(1)
        except (gitlab.GitlabError, requests.exceptions.RequestException) as e:
            fg_print.error(f"Failed to connect to GitLab! {e}")
            os.sys.exit(1)
        assert isinstance(gl.user, gitlab.v4.objects.CurrentUser)
        return gl


    @staticmethod
    def _report_rate_limit(response: requests.Response, *_args, **_kwargs) -> None:
        """requests response hook, reporting when GitLab throttles us (HTTP 429)"""
        if response.status_code != 429:
            return
        wait = response.headers.get("Retry-After")
        if wait is None and "RateLimit-Reset" in response.headers:
            try:
                wait = max(0, int(int(response.headers["RateLimit-Reset"]) - time.time()))
            except ValueError:
                wait = None
        endpoint = response.request.path_url.split("?")[0] if response.request else ""
        fg_print.warning(f"GitLab rate limit reached ({endpoint}), waiting "
                         f"{f'{wait}s' if wait is not None else 'a moment'} before retrying...")



    def test_gitlab_connection(self, gl_api:gitlab.Gitlab):
        """Run an API call to ensure the connection was successful"""
        version_tuple=gl_api.version()
        fg_print.info(
            f"Connected to GitLab, version: {version_tuple[0]}"
        )
        if version_tuple[0] == "unknown" and version_tuple[0] == "unknown":
            return False
        return True
