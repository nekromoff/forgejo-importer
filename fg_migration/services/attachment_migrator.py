"""Copies files linked in migrated issues, merge requests and comments (e.g. pasted images)
   from the source system into Forgejo, and points the links at the Forgejo copies"""
import io
import re
import time
import zipfile
from dataclasses import dataclass, field
from typing import Iterator
from urllib.parse import unquote

from pyforgejo import Comment, Issue
from pyforgejo.core.api_error import ApiError
from requests import RequestException

from fg_migration.adapters.destination_forgjo import ForgejoDestination
from fg_migration.adapters.forgeo_types import ApiPaginator, IterativeFetchError
from fg_migration.core.canonical_types import CanonicalRepo, CanonicalRepoOwner
from fg_migration.core.checkpoint import Checkpoint
from fg_migration.core.json_cache import JsonCache
from fg_migration.core.caching_source import CachedGitLabSource
from fg_migration.utils import fg_print

# GitLab upload links, as copied unchanged by the Forgejo migration, e.g.
#   /uploads/<secret>/image.png                        (relative, the usual form)
#   https://gitlab.example.com/group/project/uploads/<secret>/image.png
#   https://gitlab.example.com/-/project/123/uploads/<secret>/image.png
# Forgejo attachment links (/attachments/<uuid>) never match, so rewritten links are skipped
# returned by _upload for a file Forgejo will never accept (type or size), which is skipped
SKIPPED = object()

UPLOAD_LINK = re.compile(
    r"(?:https?://[^\s()<>\"'\[\]]*?)?/uploads/(?P<secret>[0-9a-f]{32})/"
    r"(?P<filename>[^\s()<>\"'\[\]?#]+)")


@dataclass
class _Item:
    """An issue, merge request or comment whose text links to source system uploads"""
    kind: str # issue or comment
    label: str # for messages, e.g. issue #12
    forgejo_id: int # issue number or comment id
    body: str
    updated_at: object
    existing_assets: dict[str, str] = field(default_factory=dict) # name -> url


class AttachmentMigrator:
    """Rerunnable: files are cached once downloaded, an attachment already uploaded to an
       issue or comment is reused (by name), rewritten links no longer match, and a repository
       is only marked done when all its links were migrated"""

    def __init__(self, migration_source: CachedGitLabSource, migration_dest: ForgejoDestination,
                 done_cache: JsonCache | None, checkpoint_cache: JsonCache | None = None,
                 from_start: bool = False):
        self.migration_source = migration_source
        self.migration_dest = migration_dest
        self.fg_api = migration_dest.fg_api
        self.page_size = migration_dest.forgejo_config.API_MAX_PAGE_SIZE
        self.done_cache = done_cache
        self.checkpoint = Checkpoint(checkpoint_cache, "attachments", from_start)

    def migrate_attachments(self) -> None:
        """migrate the attachments of all repositories"""
        source_name = self.migration_source.get_source_system_name()
        fg_print.section("Issue attachments (images and files)")
        fg_print.info(f"Collecting projects from {source_name}...")
        repos = self.migration_source.list_repositories()
        total = len(repos)
        start = time.time()
        resume = self.checkpoint.resume_index([str(repo.source_id) for repo in repos])
        for index, repo in enumerate(repos, start=1):
            if index <= resume:
                continue
            self.checkpoint.mark(str(repo.source_id), f"{repo.owner_name}/{repo.name}")
            done_key = f"attachments_done/{repo.source_id}"
            # --from-start also rechecks completed repositories (e.g. after allowing a file type)
            if (self.done_cache is not None and not self.checkpoint.from_start
                    and self.done_cache.get(done_key) is not None):
                fg_print.progress(index, total, f"Project {repo.owner_name}/{repo.name}:"
                                                " attachments already migrated")
                continue
            fg_print.progress(index, total, f"Project {repo.owner_name}/{repo.name}")
            if self._migrate_repo(repo) and self.done_cache is not None:
                self.done_cache.put(done_key, True)
        self.checkpoint.finish()
        fg_print.info(f"Attachments finished in {fg_print.format_duration(time.time() - start)}")

    def _migrate_repo(self, repo: CanonicalRepo) -> bool:
        """True if every link of the repository was migrated"""
        owner = self.migration_dest.resolve_forgejo_repo_owner(source_repo=repo)
        if owner is None or not owner.is_complete():
            return False
        repo_name = repo.get_safe_username()
        try:
            forgejo_repo = self.fg_api.repository.repo_get(owner=owner.username, repo=repo_name)
        except (ApiError, RequestException) as e:
            fg_print.warning(f"Repository {owner.username}/{repo_name} not found in Forgejo"
                             " (not migrated yet?), skipping its attachments:"
                             f" {self._error_detail(e)}")
            return False

        # scan first, reading works on archived repositories too
        try:
            items = list(self._iter_items_with_uploads(owner, repo_name))
        except IterativeFetchError:
            return False
        if not items:
            fg_print.info("No uploaded files linked in issues, merge requests or comments")
            return True
        links = sum(len(self._links(item.body)) for item in items)
        fg_print.info(f"Found {links} uploaded files linked in {len(items)} issues,"
                      " merge requests and comments")

        # an archived repository rejects edits, so unarchive it for the duration
        was_archived = bool(forgejo_repo.archived)
        if was_archived and not self._set_archived(owner, repo_name, False):
            return False
        try:
            all_ok = True
            for index, item in enumerate(items, start=1):
                fg_print.progress(index, len(items), f"{item.label}")
                all_ok = self._migrate_item(repo, owner, repo_name, item) and all_ok
            return all_ok
        finally:
            # also when interrupted
            if was_archived:
                self._set_archived(owner, repo_name, True)

    def _iter_items_with_uploads(self, owner: CanonicalRepoOwner,
                                 repo_name: str) -> Iterator[_Item]:
        issues = ApiPaginator(fg_api=self.fg_api, page_size=self.page_size,
                              items_type="Issues and merge requests",
                              retrieval_detail=f" for {repo_name}")
        issue: Issue
        for issue in issues.iterate(fetch_page_from_api=lambda fg_api, page, limit:
                                    fg_api.issue.list_issues(owner=owner.username, repo=repo_name,
                                                             state="all", page=page, limit=limit)):
            if issue.body and UPLOAD_LINK.search(issue.body):
                kind = "merge request" if issue.pull_request is not None else "issue"
                yield _Item(kind="issue", label=f"{kind} #{issue.number}",
                            forgejo_id=issue.number, body=issue.body,
                            updated_at=issue.updated_at,
                            existing_assets=self._asset_urls(issue.assets))

        comments = ApiPaginator(fg_api=self.fg_api, page_size=self.page_size,
                                items_type="Comments", retrieval_detail=f" for {repo_name}")
        comment: Comment
        for comment in comments.iterate(fetch_page_from_api=lambda fg_api, page, limit:
                                        fg_api.issue.get_repo_comments(owner=owner.username,
                                                                       repo=repo_name,
                                                                       page=page, limit=limit)):
            if comment.body and UPLOAD_LINK.search(comment.body):
                issue_number = (comment.issue_url or "").rstrip("/").rsplit("/", 1)[-1]
                yield _Item(kind="comment", label=f"comment {comment.id} on #{issue_number}",
                            forgejo_id=comment.id, body=comment.body,
                            updated_at=comment.updated_at,
                            existing_assets=self._asset_urls(comment.assets))

    def _migrate_item(self, repo: CanonicalRepo, owner: CanonicalRepoOwner,
                      repo_name: str, item: _Item) -> bool:
        """copy the linked files and rewrite the links. True if all links were migrated"""
        new_urls: dict[tuple[str, str], str] = {}
        all_ok = True
        for secret, filename in self._links(item.body):
            original_name = unquote(filename)
            # pasted images are nearly all called image.png, so make the name unique per
            # upload, which also lets a rerun find exactly this upload
            name = f"{secret[:12]}-{original_name}"
            # (a file type Forgejo refuses is uploaded zipped, see _upload)
            url = item.existing_assets.get(name) or item.existing_assets.get(f"{name}.zip")
            if url is not None:
                # uploaded by an interrupted earlier run
                new_urls[(secret, filename)] = url
                continue
            data = self.migration_source.download_upload(repo, secret, filename)
            if data is None:
                fg_print.error(f"{repo_name} {item.label}: unable to download {original_name} from "
                               f"{self.migration_source.get_source_system_name()} (the uploads"
                               " API needs the Maintainer role), link left unchanged",
                               f"{repo_name} {item.label}: download of {original_name} failed")
                all_ok = False
                continue
            url = self._upload(owner, repo_name, item, name, data)
            if url is SKIPPED:
                continue # not retried, the repository can still be completed
            if url is None:
                all_ok = False
                continue
            item.existing_assets[name] = url
            new_urls[(secret, filename)] = url

        if new_urls:
            body = UPLOAD_LINK.sub(
                lambda m: new_urls.get((m.group("secret"), m.group("filename")), m.group(0)),
                item.body)
            if not self._save_body(owner, repo_name, item, body):
                return False
            fg_print.info(f"{item.label}: {len(new_urls)} file(s) now stored in Forgejo")
        return all_ok

    def _upload(self, owner: CanonicalRepoOwner, repo_name: str, item: _Item,
                name: str, data: bytes) -> object:
        """the url of the attachment, SKIPPED if Forgejo refuses the file,
           None on another error (retried by the next run)"""
        try:
            return self._create_attachment(owner, repo_name, item, name, data)
        except (ApiError, RequestException) as e:
            # a file type not in Forgejo's [attachment] ALLOWED_TYPES (e.g. .eps) is refused,
            # zip files are allowed by default, so keep the file by uploading it zipped
            if isinstance(e, ApiError) and e.status_code in (400, 415, 422):
                fg_print.warning(f"{item.label}: Forgejo refused {name}"
                                 f" ({self._error_detail(e)}), uploading it as {name}.zip")
                try:
                    return self._create_attachment(owner, repo_name, item, f"{name}.zip",
                                                   self._zip(name, data))
                except (ApiError, RequestException) as e_zip:
                    e = e_zip
            if isinstance(e, ApiError) and e.status_code in (400, 413, 415, 422):
                fg_print.warning(f"{item.label}: skipping {name}, Forgejo refused it"
                                 f" ({self._error_detail(e)}). Link left unchanged, allow it via"
                                 " [attachment] ALLOWED_TYPES / MAX_SIZE in Forgejo's app.ini"
                                 " and rerun with --from-start if needed")
                return SKIPPED
            fg_print.error(f"{repo_name} {item.label}: upload of {name} failed"
                           f" ({self._error_detail(e)}), will be retried by the next run",
                           f"{repo_name} {item.label}: upload of {name} failed")
            return None

    def _create_attachment(self, owner: CanonicalRepoOwner, repo_name: str, item: _Item,
                           name: str, data: bytes) -> str | None:
        if item.kind == "issue":
            attachment = self.fg_api.issue.create_issue_attachment(
                owner=owner.username, repo=repo_name, index=item.forgejo_id,
                attachment=(name, data), name=name, updated_at=item.updated_at)
        else:
            attachment = self.fg_api.issue.create_issue_comment_attachment(
                owner=owner.username, repo=repo_name, id=item.forgejo_id,
                attachment=(name, data), name=name, updated_at=item.updated_at)
        return attachment.browser_download_url

    @staticmethod
    def _zip(name: str, data: bytes) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            # without the unique prefix, so it unpacks under its original name
            archive.writestr(name.split("-", 1)[-1], data)
        return buffer.getvalue()

    @staticmethod
    def _error_detail(e: Exception) -> str:
        """short error description, without the HTTP headers"""
        if isinstance(e, ApiError):
            body = e.body
            message = body.get("message") if isinstance(body, dict) else body
            return f"HTTP {e.status_code}: {message}"
        return str(e)

    def _save_body(self, owner: CanonicalRepoOwner, repo_name: str, item: _Item,
                   body: str) -> bool:
        # keep the original update time, the migration is not an edit
        try:
            if item.kind == "issue":
                self.fg_api.issue.edit_issue(owner=owner.username, repo=repo_name,
                                             index=item.forgejo_id, body=body,
                                             updated_at=item.updated_at)
            else:
                self.fg_api.issue.edit_comment(owner=owner.username, repo=repo_name,
                                               id=item.forgejo_id, body=body,
                                               updated_at=item.updated_at)
            return True
        except (ApiError, RequestException) as e:
            fg_print.error(f"{repo_name} {item.label}: unable to update the links:"
                           f" {self._error_detail(e)}",
                           f"{repo_name} {item.label}: link update failed")
            return False

    def _set_archived(self, owner: CanonicalRepoOwner, repo_name: str, archived: bool) -> bool:
        try:
            self.fg_api.repository.repo_edit(owner=owner.username, repo=repo_name,
                                             archived=archived)
            fg_print.info(f"Repository {owner.username}/{repo_name} "
                          + ("archived again" if archived else
                             "temporarily unarchived to update its links"))
            return True
        except (ApiError, RequestException) as e:
            fg_print.error(f"Unable to {'archive' if archived else 'unarchive'} repository"
                           f" {owner.username}/{repo_name}: {self._error_detail(e)}",
                           f"{'archive' if archived else 'unarchive'} of"
                           f" {owner.username}/{repo_name} failed")
            return False

    @staticmethod
    def _links(body: str) -> list[tuple[str, str]]:
        """unique (secret, filename) of the upload links, in order"""
        return list(dict.fromkeys((m.group("secret"), m.group("filename"))
                                  for m in UPLOAD_LINK.finditer(body)))

    @staticmethod
    def _asset_urls(assets) -> dict[str, str]:
        return {asset.name: asset.browser_download_url for asset in assets or []
                if asset.name and asset.browser_download_url}
