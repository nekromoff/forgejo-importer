"""Only contains the BaseAccessMappingStrategy, an abstract
   incomplete implementation of AccessMappingStrategy"""
from typing import override

from pyforgejo import Organization, Team

from fg_migration.adapters.destination_forgjo import ForgejoDestination
from fg_migration.adapters.forgeo_types import (ForgejoPermission, ForgejoRepositoryRole,
                                                ForgejoRolePermissionDefinition,
                                                ForgejoTeamDefinition, IterativeFetchError)
from fg_migration.core.canonical_types import CanonicalOrganization, CanonicalRepo, CanonicalRepoMemberships
from fg_migration.core.caching_source import CachedGitLabSource
from fg_migration.strategies.access_mapping_strategy import AccessMappingStrategy
from fg_migration.utils import fg_print


class BaseAccessMappingStrategy(AccessMappingStrategy):
    """A class to contain all generally useful functions"""
    migration_dest:ForgejoDestination

    def __init__(self, migration_dest:ForgejoDestination):
        self.migration_dest = migration_dest



    @override
    def resolve_forgejo_permission(
        self,
        migration_source:CachedGitLabSource,
        source_access_level: str,
    ) -> ForgejoPermission | None:

        role_definition = self._get_forgejo_role_definition(
            migration_source=migration_source,
            source_access_level=source_access_level,
        )

        if role_definition is None:
            return None

        return role_definition.permission



    def should_remove_migration_user_from_owners_team(self,
                                                migration_source:CachedGitLabSource,
                                                migration_username:str,
                                                repo_accessors:CanonicalRepoMemberships) -> bool:
        """Is the migration user not supposed to have owner permission?"""
        all_accessor_usernames: set[str] = {m.username
                                            for m in repo_accessors.members
                                            if m.username}

        migration_username = self.migration_dest.get_active_user().login
        remove_self = False
        if not migration_username in all_accessor_usernames:
            # We must remove our user account from those with access to this repo.
            remove_self = True
        else:
            migrator_access_levels: set[str] = {m.access_level
                                                for m in repo_accessors.members
                                                if m.username == migration_username}
            remove_self = True
            for access_level in migrator_access_levels:
                perm = self.resolve_forgejo_permission(migration_source=migration_source,
                                            source_access_level=access_level)
                if perm == ForgejoPermission.OWNER:
                    remove_self = False
        return remove_self



    def _get_forgejo_role_definition(self, migration_source:CachedGitLabSource,
                                     source_access_level:str,
                                     ) -> ForgejoRolePermissionDefinition | None:
        """The ForgejoRoleDefinition of a GitLab access level, None if none is mapped"""
        repository_role : ForgejoRepositoryRole = migration_source.get_repository_role(
                                                        source_access_level=source_access_level)
        if repository_role.is_custom:
            # not in GITLAB_ACCESS_LEVEL_ROLES in roles.py
            fg_print.warning(f"No Forgejo role mapped for GitLab access level"
                             f" {source_access_level} ({repository_role}), add it to"
                             " GITLAB_ACCESS_LEVEL_ROLES in roles.py")
            return None
        return self.migration_dest.role_definitions[repository_role]



    def _get_forgejo_team_definition(self,
                                     migration_source:CachedGitLabSource,
                                     source_access_level:str,
                                     ) -> ForgejoTeamDefinition | None:
        """The ForgejoTeamDefinition of a GitLab access level, None if none is mapped"""
        repository_role : ForgejoRepositoryRole = migration_source.get_repository_role(
                                                    source_access_level=source_access_level)
        if repository_role.is_custom:
            # not in GITLAB_ACCESS_LEVEL_ROLES in roles.py
            fg_print.warning(f"No Forgejo team mapped for GitLab access level"
                             f" {source_access_level} ({repository_role}), add it to"
                             " GITLAB_ACCESS_LEVEL_ROLES in roles.py")
            return None
        return self.migration_dest.team_definitions[repository_role]



    def _safely_add_new_team(self,
                             organization:CanonicalOrganization|Organization,
                             team_definition:ForgejoTeamDefinition) -> Team | None:

        name : str
        if isinstance(organization,Organization):
            name = organization.username
        else:
            name = organization.get_safe_username()

        existing_team = self.migration_dest.forgejo_add_organization_team(
            org_name=name,
            definition=team_definition,
        )

        if existing_team is None:
            fg_print.warning(f"Failed to create team {team_definition.name}")
            return None

        if not existing_team.name:
            fg_print.error(
                f"Created team returned without a name. Team Id = {existing_team.id}"
                f"for organization {organization.get_safe_username()}."
                " Skipping import of team users."
            )
            return None
        return existing_team



    @override
    def import_team_users_from_usernames(
        self,
        organization: CanonicalOrganization|Organization,
        usernames: set[str],
        dest_team: Team,
        team_members_cache: dict[int, set[str]], # map[Team.id -> {member.username}]
        is_new_team: bool,
    ):
        """Create an entry in the team for every user with a username in the set
           provided. Uses the team_members_cache to identify existing team users
           from previous operations"""

        org_name : str
        if isinstance(organization,Organization):
            org_name = organization.username
        else:
            org_name = organization.get_safe_username()

        # ---------------------------------------------
        # STEP 1: resolve existing members
        # ---------------------------------------------
        team_key = dest_team.id
        if team_key is None:
            fg_print.error(f"Team id is not set for team {dest_team.name}")
        if team_key in team_members_cache:
            existing_member_names = team_members_cache[team_key]
        else:
            if is_new_team:
                existing_member_names = set()
            else:
                try:
                    existing_member_names = {
                        m.login
                        for m in self.migration_dest.iter_forgejo_team_members(
                            team=dest_team
                        )
                        if m.login
                    }
                except IterativeFetchError:
                    fg_print.warning(
                        f"Could not fetch members for team {dest_team.name}, "
                        f"assuming empty (may cause duplicates)"
                    )
                    existing_member_names = set()

            # cache is purely optional optimisation
            team_members_cache[team_key] = existing_member_names

        # ---------------------------------------------
        # STEP 2: reconcile membership (idempotent)
        # ---------------------------------------------
        for username in usernames:

            if username in existing_member_names:
                continue

            added = self.migration_dest.forgejo_add_user_to_organization_team(
                organization_name=org_name,
                username=username,
                team=dest_team,
            )

            if added:
                existing_member_names.add(username)
            else:
                fg_print.error(
                    f"Failed to add {username} to team {dest_team.name}"
                )

    def find_owner_team_for_repo(self, source_repo:CanonicalRepo) -> Team|None:
        """Find the owner team for this repository"""

        # Get list of teams in the repository essentially.
        iter_existing_repo_teams = self.migration_dest.iter_forgejo_teams_in_repository(
                                            owner_username=source_repo.get_safe_owner_name(),
                                            repo_name=source_repo.get_safe_username(),
                                        )

        owner_team : Team
        try:
            for repo_team in iter_existing_repo_teams:
                current_team_def = ForgejoTeamDefinition.from_team(
                            team=repo_team,
                            role_builder=self.migration_dest.forgejo_team_to_role_mapper,
                            require_exact=True)

                if current_team_def.permissions.permission == ForgejoPermission.OWNER:
                    owner_team = repo_team
                    break
        except IterativeFetchError:
            fg_print.error(
                f"Failed to load organization teams for "
                f"{source_repo.get_safe_owner_name()}"
            )
            return None
        return owner_team
