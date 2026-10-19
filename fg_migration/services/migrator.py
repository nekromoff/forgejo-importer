"""Only contains the Migrator - a migration engine from system x to Forgejo"""
import time

from pyforgejo import Repository

from fg_migration.strategies.access_level_mapping_strategy import AccessLevelAccessMappingStrategy
from fg_migration.utils import fg_print
from fg_migration.strategies.access_mapping_strategy import AccessMappingStrategy
from fg_migration.adapters.forgeo_types import (ForgejoApiBuilder, ForgejoPermission,
                                                IterativeFetchError)
from fg_migration.core.caching_source import CachedSource
from fg_migration.core.canonical_types import (CanonicalOrganizations, CanonicalRepo,
                                               CanonicalSystemUser)
from fg_migration.adapters.destination_forgjo import ForgejoDestination, RepoMigrationStillRunning
from fg_migration.core.checkpoint import Checkpoint
from fg_migration.core.json_cache import JsonCache


class Migrator:
    """The migration engine, loading the data of the source (GitLab or GitHub) into Forgejo"""

    migration_dest : ForgejoDestination
    migration_source: CachedSource
    fg_api_builder : ForgejoApiBuilder
    access_mapping_strategy: AccessMappingStrategy
    # where checkpoints are kept, so a rerun of a step continues where it stopped
    checkpoint_cache: JsonCache | None = None
    from_start: bool = False

    def __init__(self, migration_source:CachedSource,
                 migration_dest:ForgejoDestination, fg_api_builder:ForgejoApiBuilder):
        self.migration_dest = migration_dest
        self.migration_source = migration_source
        self.fg_api_builder = fg_api_builder
        # one Forgejo team per source access level, see AccessLevelAccessMappingStrategy
        self.access_mapping_strategy = AccessLevelAccessMappingStrategy(
                                                    migration_dest=self.migration_dest)
        self.run_logic_checks()



    def run_logic_checks(self):
        """Run a few checks on configuration before the migration runs"""

        source_roles = self.migration_source.list_mapped_forgejo_repository_roles()
        destination_roles = self.migration_dest.role_definitions.keys()
        missing_roles = source_roles - destination_roles
        if len(missing_roles) > 0:
            fg_print.error("Migration cannot be run, the following roles are mapped from the "
                           f"source system but missing in the destination system: {missing_roles}."
                           " Please add these roles to the destination system or remove the mapping"
                           " for these roles in the migration configuration and try again.")
            raise RuntimeError("Migration cannot be run, missing mapped roles"
                               " in destination system")

        owner_count=0
        for item in self.migration_dest.role_definitions.items():
            if item[1].permission == ForgejoPermission.OWNER:
                owner_count += 1
        match owner_count:
            case 0:
                fg_print.error("A single OWNER role with owner permission MUST be defined in roles.py")
                raise ValueError()
            case 1:
                pass
            case _:
                fg_print.error("More than the permitted one OWNER role with owner permission has been defined in roles.py")
                raise ValueError()


    def close(self) -> None:
        """Close the API interface (permanent)"""



    #TODO reenable this code and update it to work (it isn't strictly required,
    #     but someone may find it useful to customise what happens normally in the auto-migrate)

    def _run_inbuilt_repo_import(self, source_repo: CanonicalRepo) -> bool:
        """Run the inbuilt import on the project.
           False if Forgejo is still migrating it in the background"""

        # get either the Forgejo User or Organization name as appropriate
        # for this source repository owner
        forgejo_owner = self.migration_dest.resolve_forgejo_repo_owner(source_repo=source_repo)

        if forgejo_owner is None:
            fg_print.error(
                f"Importing {source_repo.source_system} {source_repo.name}."
                f" Failed to locate Forgejo repository owner {source_repo.get_safe_owner_name()}"
                f" for repository {source_repo.get_safe_username()}, skipping import",
                f"Importing {source_repo.source_system} {source_repo.name}."
                f" Failed to locate Forgejo repository owner for repository "
                f"{source_repo.get_safe_username()}")
            return True


        if not forgejo_owner.is_complete():
            fg_print.error(
                f"Importing {source_repo.source_system} {source_repo.name}. Located"
                f" incomplete Forgejo repository owner {source_repo.get_safe_owner_name()}"
                f" for repository {source_repo.get_safe_username()}, skipping import",
                f"Importing {source_repo.source_system} {source_repo.name}. "
                f"Located incomplete Forgejo repository owner for repository"
                f" {source_repo.get_safe_username()}")
            return True


        if not self.migration_dest.forgejo_repo_exists(forgejo_owner=forgejo_owner,
                                                       repo=source_repo):

            fg_print.info(f"Importing {source_repo.source_system} {source_repo.source_type}"
                          f" {source_repo.name} from {source_repo.clone_url} (git data, wiki,"
                          " issues, labels, milestones, merge / pull requests, releases)."
                          " This runs on the Forgejo server and can take several minutes,"
                          " please wait...")
            migrate_start = time.time()

            try:
                imported_repo : Repository | None = self.migration_dest.repo_migrate(
                                        source_repo=source_repo,
                                        forgejo_owner=forgejo_owner,
                                        service=self.migration_source.migration_service(),
                                        issues=True,
                                        labels=True,
                                        milestones=True,
                                        mirror=False,
                                        pull_requests=True,
                                        releases=True,
                                        wiki=True,
                                )
            except RepoMigrationStillRunning as e:
                fg_print.warning(str(e))
                return False
            took = fg_print.format_duration(time.time() - migrate_start)
            if imported_repo is not None:
                fg_print.success(
                    f"{source_repo.source_system} {source_repo.source_type}"
                    f" {source_repo.get_safe_username()} imported in {took} from"
                    f" {source_repo.clone_url} and available at {imported_repo.clone_url}")
            else:
                fg_print.info(f"Import of {source_repo.get_safe_username()} ended after {took}")
        return True




    def _import_user_keys(
        self,
        user:CanonicalSystemUser,
    ):
        """import public keys for a user"""
        iter_forgejo_keys = self.migration_dest.iter_forgejo_user_keys(
                                                    username=user.get_safe_username())
        iter_forgejo_gpg_keys = self.migration_dest.iter_forgejo_user_gpg_keys(
                                                    username=user.get_safe_username())

        #
        # SSH keys
        #
        try:
            forgejo_key_values = {k.key for k in iter_forgejo_keys}
        except IterativeFetchError:
            # Allow none to be imported
            forgejo_key_values = []
        new_keys = [key
                    for key in user.keys
                    if key.key not in forgejo_key_values]
        for key in new_keys:
            # Import key
            new_key = self.migration_dest.forgejo_add_user_key(
                                                username=user.get_safe_username(),
                                                key_name=key.name, key_content=key.key)
            if new_key is not None:
                fg_print.info(f"For {user.source_system} User {user.username}"
                              f" Added new Public Key {key.name} : {new_key.key}")
            # if new_key is not None:
            #     forgejo_keys.append(new_key)

        #
        # GPG keys
        #
        try:
            forgejo_gpg_key_values = {k.public_key for k in iter_forgejo_gpg_keys}
        except IterativeFetchError:
            # Allow none to be imported
            forgejo_gpg_key_values = []
        new_gpg_keys = [key
                for key in user.gpg_keys
                if key.armored_public_key not in forgejo_gpg_key_values]
        for gpg_key in new_gpg_keys:
            # Import key
            new_key = self.migration_dest.forgejo_add_gpg_key(
                                            username=user.get_safe_username(),
                                            key_name=gpg_key.name,
                                            armored_public_key=gpg_key.armored_public_key,
                                            armored_signature=gpg_key.armored_signature)
            if new_key is not None:
                fg_print.info(f"For {user.source_system} User {user.username}"
                              f" Added new GPG Key {gpg_key.name} : {new_key.public_key}")
            # if new_key is not None:
            #     forgejo_gpg_keys.append(new_key)



    def import_users(self, notify=False):
        """import users and their public keys"""
        source_name = self.migration_source.get_source_system_name()
        fg_print.section("Users")
        fg_print.info(f"Collecting users from {source_name}, this can take a while...")
        # read all users
        users: list[CanonicalSystemUser] = self.migration_source.list_system_users()

        fg_print.info(f"Found {len(users)} {source_name} users, importing them into Forgejo")

        user : CanonicalSystemUser
        total = len(users)
        checkpoint = Checkpoint(self.checkpoint_cache, "users", self.from_start)
        start = checkpoint.resume_index([user.username for user in users])
        for index, user in enumerate(users, start=1):
            if index <= start:
                continue
            checkpoint.mark(user.username)

            fg_print.progress(index, total, f"Importing {user.source_system} user {user.username}"
                                            f" as {user.get_safe_username()}...")

            fg_print.info(f"Found {len(user.gpg_keys)} gpg keys for user {user.username}")
            fg_print.info(f"Found {len(user.keys)} public keys for user {user.username}")

            # NOTE: newly created users will have the password field
            #       updated to their new temporary password
            is_in_forgejo = self.migration_dest.forgejo_add_user(user=user, notify=notify)
            if not is_in_forgejo:
                # something went wrong with the user import. can't do any more for this user.
                continue

            if user.avatar_url:
                self._import_avatar(user)

            # import public keys if possible
            self._import_user_keys(user=user)

        # Now print all the newly created users details (so they can be
        #  copied and pasted as needed into a spreadsheet perhaps)
        checkpoint.finish()
        self._list_user_details(users=users)



    def _list_user_details(self, users:list[CanonicalSystemUser]):
        username_w = max(len("Username"), *(len(u.username) for u in users))
        password_w = max(len("Password"), *(len(u.password or "") for u in users))
        email_w = max(len("Email"), *(len(u.email or "") for u in users))

        header = (
            f"{'Username':<{username_w}}  "
            f"{'Password':<{password_w}}  "
            f"{'Email':<{email_w}}  "
            f"Full Name"
        )

        fg_print.warning("\n\nMigrated user details:\n")
        fg_print.warning(header)
        fg_print.warning("-" * len(header))

        for user in users:
            fg_print.warning(
                f"{(user.username or ''):<{username_w}}  "
                f"{(user.password or ''):<{password_w}}  "
                f"{(user.email or ''):<{email_w}}  "
                f"{user.full_name}"
            )
        fg_print.warning("\n\n")



    def _import_avatar(self, user: CanonicalSystemUser) -> None:
        """Upload the source avatar of a user (new or existing) as a local Forgejo avatar.
           Done once per avatar, so a rerun does not overwrite an avatar changed in Forgejo"""
        done_key = f"avatars/{user.get_safe_username()}"
        if (self.checkpoint_cache is not None
                and self.checkpoint_cache.value(done_key) == user.avatar_url):
            fg_print.debug(f"Avatar of user {user.username} already imported")
            return

        image = self.migration_source.download_avatar(user.avatar_url)
        if image is None:
            # also removes a generated gravatar picture an earlier version uploaded
            if not self.migration_dest.forgejo_delete_user_avatar(user.get_safe_username()):
                return # retried by the next run
            fg_print.info(f"User {user.username} has no avatar (none uploaded to"
                          f" {self.migration_source.get_source_system_name()}, no gravatar),"
                          " Forgejo's default avatar is used")
        elif self.migration_dest.forgejo_set_user_avatar(user.get_safe_username(), image):
            fg_print.info(f"Imported avatar for user {user.username}")
        else:
            return # retried by the next run
        if self.checkpoint_cache is not None:
            self.checkpoint_cache.put(done_key, user.avatar_url)


    def import_organizations(self):
        """import all organizations and their members"""
        fg_print.section("Organizations")
        fg_print.info(f"Collecting organizations from {self.migration_source.get_source_system_name()},"
                      " this can take a while...")
        # read all users
        canonical_organizations: CanonicalOrganizations = self.migration_source.list_organizations()

        fg_print.info(f"Found {len(canonical_organizations.members)} "
                      f"{self.migration_source.get_source_system_name()}"
                      f" {canonical_organizations.source_type}")

        org_names = [org.username for org in canonical_organizations.members]
        fg_print.info(f"Importing organizations... {org_names}")


        total = len(canonical_organizations.members)
        checkpoint = Checkpoint(self.checkpoint_cache, "groups", self.from_start)
        start = checkpoint.resume_index([org.username for org in canonical_organizations.members])
        for index, organization in enumerate(canonical_organizations.members, start=1):
            if index <= start:
                continue
            checkpoint.mark(organization.username)
            # create the Forgejo organization
            fg_print.progress(index, total,
                              f"Importing {organization.source_type} {organization.username}"
                              f" as Forgejo organization {organization.get_safe_username()}...")

            # This individual retrieval replaces a search through the list. I'm not sure
            # if it'll use more time and be less reliable (more api calls)...
            existing_forgejo_org = self.migration_dest.get_forgejo_organization(org=organization, quiet=True)

            # fg_print.debug(f"Existing Forgejo organizations: "
            #                f"{[org.username for org in existing_forgejo_organizations]}")
            fg_print.debug(f"Matched existing Organization = {existing_forgejo_org is not None}")
            # Add the forgejo organization
            added_org = self.migration_dest.forgejo_add_organization(
                                                organization=organization,
                                                existing_forgejo_org=existing_forgejo_org)
            if not added_org:
                fg_print.error("Skipping adding teams for Organization "
                               f"{organization.get_safe_username()} that does not exist",
                               "Skipping adding teams for Organization "
                               f"{organization.get_safe_username()} that does not exist")
                continue # org does not exist

            # Finally, import the organization members
            fg_print.info(f"Importing teams and members of {organization.get_safe_username()}...")
            self.access_mapping_strategy.import_teams(migration_source=self.migration_source,
                                                      organization=organization)



    def import_repos(self, import_repo_content:bool=True):
        """read all projects and their issues if import_repo_content is True.
           Always applies Collabroration rights"""

        fg_print.section("Projects / Repositories" if import_repo_content
                         else "Project membership")
        fg_print.info(f"Collecting projects from {self.migration_source.get_source_system_name()},"
                      " this can take a while...")
        source_repos : list[CanonicalRepo] = self.migration_source.list_repositories()
        fg_print.info(f"Found {len(source_repos)} projects")

        source_repo : CanonicalRepo
        total = len(source_repos)
        checkpoint = Checkpoint(self.checkpoint_cache,
                                "projects" if import_repo_content else "membership",
                                self.from_start)
        start = checkpoint.resume_index([str(repo.source_id) for repo in source_repos])
        for index, source_repo in enumerate(source_repos, start=1):
            if index <= start:
                continue
            checkpoint.mark(str(source_repo.source_id),
                            f"{source_repo.owner_name}/{source_repo.name}")
            fg_print.progress(index, total, f"Project {source_repo.owner_name}/{source_repo.name}"
                                            + (" (archived)" if source_repo.is_archived else ""))
            still_migrating = False

            if import_repo_content:
                if source_repo.is_individual:
                    fg_print.info(f"Importing personal {source_repo.source_type}"
                                  f" {source_repo.name} from owner {source_repo.owner_name}")
                else:
                    fg_print.info(f"Importing shared {source_repo.source_type} {source_repo.name}"
                                  f" from owner {source_repo.owner_name}")

                # import project repo
                if not self._run_inbuilt_repo_import(source_repo=source_repo):
                    still_migrating = True

            # Always configure the accessors.
            fg_print.info(f"Applying access rights (members, teams) for {source_repo.name}...")
            self.access_mapping_strategy.import_repository_accessors(
                                                migration_source=self.migration_source,
                                                source_repo=source_repo)

            # archive last, an archived Forgejo repository rejects some changes
            if source_repo.is_archived and still_migrating:
                fg_print.warning(f"Not archiving {source_repo.name} while Forgejo is still"
                                 " migrating it, rerun with --membership later to archive it")
            elif source_repo.is_archived:
                forgejo_owner = self.migration_dest.resolve_forgejo_repo_owner(
                                                                    source_repo=source_repo)
                if forgejo_owner is not None and forgejo_owner.is_complete():
                    self.migration_dest.forgejo_archive_repo(forgejo_owner=forgejo_owner,
                                                             repo=source_repo)
        checkpoint.finish()
