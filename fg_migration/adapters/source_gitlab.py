"""GitLab Migration source definition"""
from copy import copy
import os
import re
from typing import Any, Callable, Iterator

import gitlab  # pip install python-gitlab
import requests
import gitlab.v4 # pylint: disable=no-name-in-module
import gitlab.v4.objects

from fg_migration.adapters.gitlab_types import GitLabApiPaginator, IterativeFetchError
from fg_migration.utils import fg_print
from fg_migration.adapters.destination_forgjo import ForgejoRepositoryRole
from fg_migration.core.canonical_types import (CanonicalGpgKey, CanonicalOrganizationMembership,
                                               CanonicalKey, CanonicalOrganization,
                                               CanonicalOrganizations, CanonicalRepo,
                                               CanonicalRepoMembership, CanonicalRepoMemberships,
                                               CanonicalSystemUser, CanonicalUser, HierarchyNode)
from fg_migration.core.config_types import GitLabMigrationConfig, GitLabConfig
from fg_migration.core.roles import GITLAB_ACCESS_LEVEL_ROLES
from fg_migration.core.json_cache import JsonCache

class GitLabMigrationSource:
    """Reads the data to migrate from GitLab.
       Note that all lists are retrieved in one operation, without paging"""

    gitlab_api: gitlab.Gitlab
    gitlab_config: GitLabConfig
    gitlab_migration_config: GitLabMigrationConfig
    source_system: str = "GitLab"
    access_level_role_map : dict[int,ForgejoRepositoryRole]
    BOT_REGEX = re.compile(r"^project_\d{2}_bot_[a-zA-Z0-9]{32}$")
    # number of users about to be listed, when known up front (used for progress output)
    _expected_user_count : int | None = None
    # when set, every fetched item (users, members, groups) is cached immediately
    item_cache : JsonCache | None = None
    _project_paths : dict


    def __init__(self,
                 gitlab_api:gitlab.Gitlab,
                 gitlab_config:GitLabConfig,
                 gitlab_migration_config:GitLabMigrationConfig):
        self.gitlab_api = gitlab_api
        self.gitlab_config = gitlab_config
        self._project_paths = {}
        self.gitlab_migration_config = gitlab_migration_config
        self.access_level_role_map = self._load_access_levels_roles_map()

    def _load_access_levels_roles_map(self) -> dict[int,ForgejoRepositoryRole]:
        """GitLab access level -> Forgejo role, see roles.py"""
        access_level_role_map : dict[int,ForgejoRepositoryRole] = {}

        for gitlab_access_level, role_id in GITLAB_ACCESS_LEVEL_ROLES.items():
            access_level_role_map[gitlab_access_level] = ForgejoRepositoryRole(role_id)

        return access_level_role_map



    def _iter_all_emails_of_user(self,
                                 user: gitlab.v4.objects.User
                                 ) -> Iterator[gitlab.v4.objects.UserEmail]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api,
                                       page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="UserEmails")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: user.emails.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_projects(self) -> Iterator[gitlab.v4.objects.Project]:
        # without membership, a non admin gets every public project of the instance
        # (on gitlab.com that is millions), so only list projects we are a member of
        membership_only = not self._is_gitlab_admin()
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api,
                                       page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="Projects")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: gl_api.projects.list(
                page=page,
                per_page=limit,
                membership=membership_only,
            )
        )



    def _iter_all_groups_of_project(self,
                                    project: gitlab.v4.objects.Project
                                    ) -> Iterator[gitlab.v4.objects.Group]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api,
                                       page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="Project Groups")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: project.groups.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_groups(self) -> Iterator[gitlab.v4.objects.Group]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api, page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="Groups")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: gl_api.groups.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_sub_groups_of_group(self, group:
                                   gitlab.v4.objects.Group
                                   ) -> Iterator[gitlab.v4.objects.GroupSubgroup]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api, page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="SubGroups")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: group.subgroups.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_descendant_groups_of_group(self, group:
                                   gitlab.v4.objects.Group
                                   ) -> Iterator[gitlab.v4.objects.GroupDescendantGroup]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api, page_size=self.gitlab_config.API_MAX_PAGE_SIZE,
                                       items_type="DescendantGroups")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: group.descendant_groups.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_members_of_group(self, group:
                                   gitlab.v4.objects.Group
                                   ) -> Iterator[gitlab.v4.objects.GroupMember]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api, page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="Groups")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: group.members.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_members_of_project(self,
                                     project: gitlab.v4.objects.Project
                                     ) -> Iterator[gitlab.v4.objects.ProjectMember]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api,
                                       page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="Project Members")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: project.members.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_user_refs(self) -> Iterator[tuple[int, str, Callable[[], gitlab.v4.objects.User]]]:
        """(user id, username, loader of the user) for all users to migrate.
           The loader is only called for users that are not cached yet"""
        if not self._is_gitlab_admin():
            # a non admin sees every user of the instance (on gitlab.com, strangers),
            # so only take the members of our projects and groups
            yield from self._iter_user_refs_of_projects_and_groups()
            return

        paginator = GitLabApiPaginator(gl_api=self.gitlab_api, page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="Users")
        for user in paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: gl_api.users.list(
                page=page,
                per_page=limit,
            )
        ):
            yield user.id, user.username, lambda user=user: user



    def _cache_get(self, key: str) -> Any:
        """cached item or None"""
        if self.item_cache is None:
            return None
        hit = self.item_cache.get(key)
        return hit[0] if hit is not None else None

    def _cache_put(self, key: str, data: Any) -> None:
        if self.item_cache is not None:
            self.item_cache.put(key, data)



    def _iter_user_refs_of_projects_and_groups(self) -> Iterator[tuple[int, str, Callable[[], gitlab.v4.objects.User]]]:
        """Users who are members (direct or inherited) of the migrated projects and groups"""
        fg_print.info("Collecting users from the members of your GitLab projects and groups...")
        member_ids : dict[int,str] = {}
        fg_print.info("Listing your GitLab projects...")
        sources = [(project, f"Project {project.path_with_namespace} Members",
                    f"members/project_{project.id}")
                   for project in self._iter_all_projects()]
        fg_print.info("Listing your GitLab groups...")
        sources += [(group, f"Group {group.full_path} Members", f"members/group_{group.id}")
                    for group in self._iter_all_groups()]
        total = len(sources)
        for index, (project_or_group, items_type, cache_key) in enumerate(sources, start=1):
            cached = self._cache_get(cache_key)
            if cached is not None:
                fg_print.progress(index, total, f"{items_type} (cached)")
                for user_id, username in cached:
                    member_ids.setdefault(user_id, username)
                continue
            fg_print.progress(index, total, f"Reading {items_type}"
                                            f" ({len(member_ids)} users found so far)")
            try:
                members = [(member.id, member.username) for member in
                           self._iter_all_members_all_of(project_or_group, items_type)]
            except IterativeFetchError:
                fg_print.error(f"Skipping users of {items_type}, rerun the import for Users")
                continue
            self._cache_put(cache_key, members)
            for user_id, username in members:
                member_ids.setdefault(user_id, username)
        fg_print.info(f"Found {len(member_ids)} distinct users in your GitLab projects and groups")
        self._expected_user_count = len(member_ids)

        for user_id, username in member_ids.items():
            # members lack the user managers (keys, gpgkeys), so load the full user
            yield user_id, username, lambda user_id=user_id: self.gitlab_api.users.get(user_id)



    def _iter_all_members_all_of(self, project_or_group, items_type:str) -> Iterator:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api,
                                       page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type=items_type)
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: project_or_group.members_all.list(
                page=page,
                per_page=limit,
            )
        )


    def _iter_all_gpg_keys_of_user(self,
                                   user:gitlab.v4.objects.User
                                   ) -> Iterator[gitlab.v4.objects.UserGPGKey]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api,
                                       page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="UserGPGKeys")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: user.gpgkeys.list(
                page=page,
                per_page=limit,
            )
        )



    def _iter_all_public_keys_of_user(self,
                                      user:gitlab.v4.objects.User
                                      ) -> Iterator[gitlab.v4.objects.UserKey]:
        paginator = GitLabApiPaginator(gl_api=self.gitlab_api, page_size=self.gitlab_config.API_MAX_PAGE_SIZE, items_type="UserKeys")
        return paginator.iterate(fetch_page_from_api=
            lambda gl_api, page, limit: user.keys.list(
                page=page,
                per_page=limit,
            )
        )



    def _get_is_individual(self, project : gitlab.v4.objects.Project) -> bool:
        namespace_kind = project.namespace.get("kind")
        if namespace_kind == "user":
            return True
        elif namespace_kind == "group":
            return False
        else:
            fg_print.error(f"Unsupported namespace kind {project.namespace['kind']}"
                           f" for project {project.name}, skipping import")
            raise ValueError(f"namespace unsupported {project.namespace['kind']}")



    def _get_gitlab_project_owner_slug(self, project: gitlab.v4.objects.Project) -> str | None:

        if project.namespace["kind"] == "user":
            # Needs to be path here because it is the stable id used for gitlab
            return project.namespace["path"]

        if project.namespace["kind"] == "group":
            # Needs to be path here because it is the stable id used for gitlab
            return project.namespace["path"]

        fg_print.error(f"Unsupported namespace kind {project.namespace['kind']}"
                       f" for project {project.name}, skipping import")
        return None



    def _is_ignore_gitlab_user(self, username : str) -> bool:
        if (username in self.gitlab_migration_config.IGNORED_GITLAB_SYSTEM_USERS
            or self.BOT_REGEX.match(username)):
            return True

        fg_print.debug(f"username {username} not in ignored users list "
                       f"{list(self.gitlab_migration_config.IGNORED_GITLAB_SYSTEM_USERS)}"
                       " and does not match bot regex, will not ignore")
        return False



    def _build_or_extract_email(self, user: gitlab.v4.objects.User) -> str:
        """build an email address for a user, if the email is not available,
           we use a dummy email address based on the username"""

        # Some gitlab instances do not publish user emails, so we use a dummy email

        # only visible to instance administrators
        email = getattr(user, "email", None)
        if email:
            return email

        # visible to anyone, if the user has chosen a public email in their profile.
        # The users list endpoint may omit it, so fall back to the single user endpoint
        public_email = getattr(user, "public_email", None)
        if public_email is None:
            try:
                public_email = getattr(self.gitlab_api.users.get(user.id),
                                       "public_email", None)
            except gitlab.GitlabError as e:
                fg_print.debug(f"Failed to load profile of user {user.username}: {e}")
        if public_email:
            fg_print.info(f"Using public email of user {user.username}")
            return public_email

        # the emails endpoint is admin only, calling it otherwise just yields 403 errors
        if self._is_gitlab_admin():
            try:
                emails = list(self._iter_all_emails_of_user(user))
                if emails:
                    return emails[0].email
            except (AttributeError, IterativeFetchError):
                pass

        placeholder = f"{user.username}@noemail-git.local"
        fg_print.warning(f"No email available for user {user.username} (needs a GitLab admin "
                         f"token, or a public email in the user's profile), using {placeholder}")
        return placeholder



    def _is_gitlab_admin(self) -> bool:
        """True if the GitLab token belongs to an instance administrator
           (never the case on gitlab.com)"""
        return bool(getattr(self.gitlab_api.user, "is_admin", False))



    def get_source_system_name(self) -> str:
        return self.source_system



    def list_mapped_forgejo_repository_roles(self) -> set[ForgejoRepositoryRole]:
        return set(self.access_level_role_map.values())



    def list_repositories(self) -> list[CanonicalRepo]:
        # projects: list[gitlab.v4.objects.Project] = self.gitlab_api.projects.list(get_all=True)
        # fg_print.info(f"Found {len(projects)} gitlab projects"
        #               f" as user {self.gitlab_api.user.username}")

        repos : list[CanonicalRepo] = []
        project : gitlab.v4.objects.Project
        seen_repos_map: dict[int,str] = {}
        fg_print.info(f"Listing your {self.source_system} projects...")
        try:
            for index, project in enumerate(self._iter_all_projects(), start=1):
                fg_print.progress(index, None, f"Reading project {project.path_with_namespace}")
                if project.get_id() in seen_repos_map:
                    fg_print.error(f"Already have reference to Project "
                                   f"{seen_repos_map[project.get_id()]} with ID {project.get_id()}")
                    continue

                owner_name = self._get_gitlab_project_owner_slug(project)

                if owner_name is None:
                    continue # unable to continue with this project

                is_individual = self._get_is_individual(project)
                # clone_url = project.web_url

                repo_name = self._get_gitlab_repo_name(project)
                if repo_name is None:
                    fg_print.error(f"Could not extract repository name for project {project.name}"
                                   f" with path {project.path_with_namespace}, skipping import")
                    continue
                clone_url = self._build_gitlab_repo_url(owner_name, repo_name)

                is_private = (project.visibility == "private" or project.visibility == "internal")
                # the token is enough for Forgejo to clone and read the GitLab API
                auth_password=None
                auth_username=None
                auth_token=self.gitlab_config.GITLAB_TOKEN

                fg_print.debug(f"{self.source_system} Project name {project.path} : path {project.name}")
                repo = CanonicalRepo(source_system=self.source_system,
                                     source_id=project.get_id(),
                                     is_individual=is_individual,
                                     username=project.path,
                                     name=project.name,
                                     owner_name=owner_name,
                                     clone_url=clone_url,
                                     is_private=is_private,
                                     description=project.description,
                                     auth_username=auth_username,
                                     auth_password=auth_password,
                                     auth_token=auth_token,
                                     source_type="Project",
                                     is_archived=bool(getattr(project, "archived", False)))
                repos.append(repo)
                seen_repos_map[repo.source_id] = repo.name
        except IterativeFetchError:
            fg_print.error("Failed to load all Projects. "
                           "Import will need to be run again for Repositories")
        return repos



    def _get_gitlab_repo_name(self, project: gitlab.v4.objects.Project) -> str|None:
        proj_path = project.path_with_namespace

        fg_print.debug(f"retrieving {self.source_system} project: {proj_path}")

        path_parts = proj_path.split("/", 1)

        if len(path_parts) != 2:
            fg_print.error(
                f"Invalid repository path: {proj_path}"
            )
            return None
        return path_parts[1]



    def _build_gitlab_repo_url(self, owner: str, repo: str) -> str:
        # https, Forgejo authenticates with the token
        return f"{self.gitlab_config.GITLAB_URL}/{owner}/{repo}.git"



    def _list_repository_accessors_inherited(self,
                                        project: gitlab.v4.objects.Project,
                                        repository:CanonicalRepo) -> list[CanonicalRepoMembership]:
        """List all those repository accessors that are inherited from a
           group in the hierarchy to which the project belongs"""

        repo_accessors_members : list[CanonicalRepoMembership] = []
        try:
            for group in self._iter_all_groups_of_project(project):
                # retrieve the full group object so we can get the members.
                group = self.gitlab_api.groups.get(group.get_id())
                root_hierarchy = [HierarchyNode(name=group.path)]
                repo_accessors_members += self._process_group(repository=repository,
                                                              group=group,
                                                              hierarchy=root_hierarchy)

        except IterativeFetchError:
            fg_print.error(f"Failed to load all Groups for Project {project.path}."
                           " Import will need to be run again for Repository Accessors")
        return repo_accessors_members


    def _process_group(self, repository : CanonicalRepo,
                       group:gitlab.v4.objects.Group,
                       hierarchy : list[HierarchyNode]
                       ) -> list[CanonicalRepoMembership]:
        """A recursive trawler of groups, descendant and sub groups"""
        group_id = group.get_id()
        repo_accessors_members : list[CanonicalRepoMembership] = []
        # retrieve the full group object so we can get the members.
        group = self.gitlab_api.groups.get(group_id)

        fg_print.debug(f"Loading inherited users from owner group {group_id}")
        # For every user that has access to this group
        try:
            for group_member in self._iter_all_members_of_group(group=group):
                if self._is_ignore_gitlab_user(group_member.username):
                    if self.gitlab_migration_config.IGNORE_GITLAB_SYSTEM_USERS is True:
                        fg_print.warning(f"Ignored a GitLab specific system user "
                                            f"{group_member.username}. If this is incorrect"
                                            ", rerun import permitting system user cloning")
                        continue
                    else:
                        fg_print.warning(f"Likely a GitLab specific system user "
                                            f"{group_member.username}. "
                                            "Can possibly be deleted after import")

                repo_accessors_members.append(CanonicalRepoMembership(
                                                username = group_member.username,
                                                repository = repository,
                                                hierarchy=hierarchy,
                                                access_level = group_member.access_level))

            depth = len(hierarchy)
            if depth <= self.gitlab_config.MAX_DESCENDANT_GROUP_DEPTH:

                for descendant_group in self._iter_all_descendant_groups_of_group(group=group):
                    desc_hierarchy = copy(hierarchy)
                    desc_hierarchy.append(HierarchyNode(
                                                name=descendant_group.path,
                                                relation=HierarchyNode.RelationEnum.DESCENDANT))
                    descendant_group_id = descendant_group.get_id()
                    descendant_group = self.gitlab_api.groups.get(descendant_group_id)
                    repo_accessors_members += self._process_group(
                                                        repository=repository,
                                                        group=descendant_group,
                                                        hierarchy=desc_hierarchy)
            if depth <= self.gitlab_config.MAX_SUB_GROUP_DEPTH:
                for sub_group in self._iter_all_sub_groups_of_group(group=group):
                    sub_hierarchy = copy(hierarchy)
                    sub_hierarchy.append(HierarchyNode(
                                                name=sub_hierarchy.path,
                                                relation=HierarchyNode.RelationEnum.SUB))
                    sub_group_id = sub_group.get_id()
                    sub_group = self.gitlab_api.groups.get(sub_group_id)
                    repo_accessors_members += self._process_group(
                                                        repository=repository,
                                                        group=sub_group,
                                                        hierarchy=sub_hierarchy)

        except IterativeFetchError:
            fg_print.error(f"Failed to load all Group Members for Group {group.name}."
                            " Import will need to be run again for Repository Accessors")
        return repo_accessors_members



    def _list_repository_accessors_direct(self,
                                        project: gitlab.v4.objects.Project,
                                        repository:CanonicalRepo) -> list[CanonicalRepoMembership]:
        """List all those repository accessors that are directly added to this project"""
        repo_accessors_members : list[CanonicalRepoMembership] = []

        project_member : gitlab.v4.objects.ProjectMember

        try:
            for project_member in self._iter_all_members_of_project(project=project):
                if self._is_ignore_gitlab_user(project_member.username):
                    if self.gitlab_migration_config.IGNORE_GITLAB_SYSTEM_USERS is True:
                        fg_print.warning("Ignored a GitLab specific system user "
                                         f"{project_member.username}. If this is incorrect, rerun "
                                         "import permitting system user cloning")
                        continue
                    else:
                        fg_print.warning("Likely a GitLab specific system user "
                                         f"{project_member.username}. "
                                         "Can possibly be deleted after import")
                fg_print.debug(f"Added accessor {project_member.username}"
                               f" for project {project.path}")
                repo_accessors_members.append(CanonicalRepoMembership(
                                                        username = project_member.username,
                                                        repository =repository,
                                                        hierarchy=None,
                                                        access_level = project_member.access_level))
        except IterativeFetchError:
            fg_print.error(f"Failed to load all Project Members for Project {project.path}."
                           " Import will need to be run again for Repository Accessors")
        # always return what we have.
        return repo_accessors_members


    def list_repository_accessors(self, repo:CanonicalRepo) -> CanonicalRepoMemberships:
        # gitlab project = forgejo repo
        project: gitlab.v4.objects.Project = self.gitlab_api.projects.get(id=repo.source_id)
        fg_print.debug(f"Listing accessors for project id {repo.source_id}, "
                       f"project {project.path}  [{project.name}]")
        repo_accessors_members : list[CanonicalRepoMembership] = []
        repo_accessors = CanonicalRepoMemberships(source_system=self.source_system,
                                                  source_type="Users",
                                                  members=repo_accessors_members)

        if not repo.is_individual:
            # These are INHERITED accessors (of the gitlab group that owns this project)
            repo_accessors_members += self._list_repository_accessors_inherited(project=project,
                                                                                repository=repo)

        # These are DIRECT accessors
        repo_accessors_members += self._list_repository_accessors_direct(project=project,
                                                                         repository=repo)

        return repo_accessors



    def _build_organization_from_group(self,group:gitlab.v4.objects.Group) -> CanonicalOrganization:

        org_users: dict[str, CanonicalUser] = {}
        memberships: list[CanonicalOrganizationMembership] = []

        try:

            for member in self._iter_all_members_of_group(group=group):

                # handle ignored users
                if self._is_ignore_gitlab_user(member.username):
                    if self.gitlab_migration_config.IGNORE_GITLAB_SYSTEM_USERS is True:
                        fg_print.warning(
                            f"Ignored GitLab system user {member.username}"
                        )
                        continue
                    else:
                        fg_print.warning(
                            f"Likely GitLab system user {member.username}"
                        )

                # store user (deduplicated)
                if member.username not in org_users:
                    org_users[member.username] = CanonicalUser(
                        username=member.username
                    )

                # store membership FACT (this is the important part)
                memberships.append(
                    CanonicalOrganizationMembership(
                        org_username=group.path,
                        username=member.username,
                        access_level=str(member.access_level),
                    )
                )

        except IterativeFetchError:
            fg_print.error(
                f"Failed to load members for {self.source_system} "
                f"Group {group.path}. Skipping group membership capture."
            )
            return None

        this_org = CanonicalOrganization(
            source_system=self.source_system,
            source_type="Group",
            username=group.path,
            full_name=group.full_name,
            description=group.description,
            members=list(org_users.values()),
            memberships=memberships,
        )

        return this_org



    def list_organizations(self) -> CanonicalOrganizations:
        organizations = CanonicalOrganizations(
            source_type="Groups",
            members=[]
        )

        fg_print.info(f"Listing your {self.source_system} groups...")
        try:
            for index, group in enumerate(self._iter_all_groups(), start=1):

                fg_print.debug(
                    f"name={group.name} path={group.path} fullname={group.full_name}"
                )

                cache_key = f"groups/{group.id}"
                canonical_org = self._cache_get(cache_key)
                if canonical_org is not None:
                    fg_print.progress(index, None, f"Group {group.full_path} (cached)")
                else:
                    fg_print.progress(index, None,
                                      f"Reading group {group.full_path} and its members")
                    canonical_org = self._build_organization_from_group(group=group)
                    if canonical_org is None:
                        continue # failed to load the members, error already reported
                    self._cache_put(cache_key, canonical_org)
                organizations.members.append(canonical_org)


        except IterativeFetchError:
            fg_print.error(
                "Failed to load all Groups. Import incomplete for Organizations."
            )

        return organizations



    def list_system_users(self) -> list[CanonicalSystemUser]:
        # users: list[gitlab.v4.objects.User] = self.gitlab_api.users.list(get_all=True)
        canonical_users : list[CanonicalSystemUser] = []
        fg_print.info(f"Listing {self.source_system} users...")
        try:
            for index, (user_id, username, load_user) in enumerate(self._iter_all_user_refs(),
                                                                    start=1):
                if self._is_ignore_gitlab_user(username):
                    if self.gitlab_migration_config.IGNORE_GITLAB_SYSTEM_USERS is True:
                        fg_print.warning(f"Ignored a GitLab specific system user {username}."
                                         " If this is incorrect, rerun import permitting"
                                         " system user cloning")
                        continue
                    else:
                        fg_print.warning(f"Likely a GitLab specific system user {username}."
                                         " Can possibly be deleted after import")

                cache_key = f"users/{user_id}"
                cached_user = self._cache_get(cache_key)
                if cached_user is not None:
                    fg_print.progress(index, self._expected_user_count, f"User {username} (cached)")
                    canonical_users.append(cached_user)
                    continue

                fg_print.progress(index, self._expected_user_count,
                                  f"Reading user {username} (email, SSH and GPG keys)")
                try:
                    user = load_user()
                except gitlab.GitlabError as e:
                    fg_print.error(f"Failed to load GitLab user {username}: {e}")
                    continue

                # gpg_keys : list[gitlab.v4.objects.UserGPGKey] = user.gpgkeys.list(get_all=True)
                # keys: list[gitlab.v4.objects.UserKey] = user.keys.list(get_all=True)

                avatar_url : str | None
                try:
                    avatar_url = user.avatar_url
                except AttributeError:
                    avatar_url = None

                email_address : str = self._build_or_extract_email(user)
                try:
                    canonical_keys = [CanonicalKey(name=key.title, key=key.key)
                                      for key in self._iter_all_public_keys_of_user(user)]

                    canonical_gpg_keys : list[CanonicalGpgKey] = []
                    for gpg_key in self._iter_all_gpg_keys_of_user(user):
                        key_id = getattr(gpg_key, "key_id", None)

                        key_content = (getattr(gpg_key, "public_key", None)
                                    or getattr(gpg_key, "key", None))
                        canonical_gpg_keys.append(CanonicalGpgKey(name=key_id,
                                                                  armored_public_key=key_content,
                                                                  armored_signature=None))
                except IterativeFetchError:
                    fg_print.error("Failed to load keys for User, User will not be imported and "
                                   "import will need to be run again for Users.")
                    continue

                canonical_user = CanonicalSystemUser(gpg_keys=canonical_gpg_keys,
                                                     keys=canonical_keys,
                                                     email=email_address,
                                                     full_name=user.name,
                                                     password=None,
                                                     username=user.username,
                                                     source_system=self.source_system,
                                                     avatar_url=avatar_url)
                canonical_users.append(canonical_user)
                # saved right away, so an interrupted run does not fetch this user again
                self._cache_put(cache_key, canonical_user)
        except IterativeFetchError:
            fg_print.error("Failed to load all Users, import will need to be run again for Users.")

        return canonical_users



    def download_upload(self, repo:CanonicalRepo, secret:str, filename:str) -> bytes | None:
        # the uploads API needs the Maintainer role in the project
        try:
            response = self.gitlab_api.http_get(
                f"/projects/{repo.source_id}/uploads/{secret}/{filename}", raw=True)
            return response.content
        except gitlab.GitlabError as e:
            fg_print.debug(f"Uploads API refused {secret}/{filename}: {e}")

        # the web url works without the Maintainer role, at least for public projects
        project_path = self._project_paths.get(repo.source_id)
        if project_path is None:
            try:
                project_path = self.gitlab_api.projects.get(repo.source_id).path_with_namespace
                self._project_paths[repo.source_id] = project_path
            except gitlab.GitlabError as e:
                fg_print.debug(f"Failed to load project {repo.source_id}: {e}")
                return None
        url = (f"{self.gitlab_config.GITLAB_URL.rstrip('/')}/{project_path}"
               f"/uploads/{secret}/{filename}")
        try:
            response = self.gitlab_api.session.get(url, timeout=self.gitlab_config.REQUEST_TIMEOUT)
        except requests.RequestException as e:
            fg_print.debug(f"Failed to download {url}: {e}")
            return None
        # a private project answers with the sign in page instead of the file
        if response.ok and "text/html" not in response.headers.get("Content-Type", ""):
            return response.content
        return None



    def get_repository_role(self, source_access_level:str) -> ForgejoRepositoryRole:
        """Get a predefined ForgejoRepositoryRole or give a unique string to identify a role type
           that should be used for this access level"""
        gitlab_access_level : int = int(source_access_level)
        role = self.access_level_role_map.get(gitlab_access_level)
        if role is None:
            fg_print.error(f"{self.source_system} Access_Level:Role "
                           f"Mapping missing for {source_access_level}")
            role_id = f"{self.source_system}_Role_{source_access_level}"
            fg_print.info(f"Created new {self.source_system} role type : {role_id}")
            role = ForgejoRepositoryRole(id=role_id, is_custom=True)
            self.access_level_role_map[gitlab_access_level] = role
        return role
