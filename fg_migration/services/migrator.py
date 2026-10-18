"""Only contains the Migrator - a migration engine from system x to Forgejo"""
import base64
import time

from pyforgejo import Repository
import requests

from fg_migration.strategies.access_level_mapping_strategy import AccessLevelAccessMappingStrategy
from fg_migration.utils import fg_print
from fg_migration.strategies.access_mapping_strategy import AccessMappingStrategy
from fg_migration.adapters.forgeo_types import (ForgejoApiBuilder, ForgejoPermission,
                                                IterativeFetchError)
from fg_migration.core.caching_source import CachedGitLabSource
from fg_migration.core.canonical_types import (CanonicalOrganizations, CanonicalRepo,
                                               CanonicalSystemUser)
from fg_migration.adapters.destination_forgjo import ForgejoDestination, RepoMigrationStillRunning
from fg_migration.core.checkpoint import Checkpoint
from fg_migration.core.json_cache import JsonCache


class Migrator:
    """The migration engine, loading the data of the GitLab source into Forgejo"""

    migration_dest : ForgejoDestination
    migration_source: CachedGitLabSource
    fg_api_builder : ForgejoApiBuilder
    access_mapping_strategy: AccessMappingStrategy
    # where checkpoints are kept, so a rerun of a step continues where it stopped
    checkpoint_cache: JsonCache | None = None
    from_start: bool = False

    def __init__(self, migration_source:CachedGitLabSource,
                 migration_dest:ForgejoDestination, fg_api_builder:ForgejoApiBuilder):
        self.migration_dest = migration_dest
        self.migration_source = migration_source
        self.fg_api_builder = fg_api_builder
        # one Forgejo team per GitLab access level, see AccessLevelAccessMappingStrategy
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

    # def _import_project_labels(
    #     migration_dest: ForgejoDestination,
    #     labels: list[gitlab.v4.objects.ProjectLabel],
    #     project_owner: str,
    #     project_name: str,
    # ):
    #     forgejo_safe_project_owner_name = name_clean(project_owner)
    #     forgejo_safe_project_name = name_clean(project_name)
    #     """import labels for a repository"""
    #     for label in labels:
    #         if not self.migration_dest.forgejo_label_exists(owner=forgejo_safe_project_owner_name,
    #              repo=forgejo_safe_project_name, labelname=label.name):
    #             # need this if block because status 422 returned for conflict, not 409
    #             try:
    #                 self.migration_dest.fg_api.issue.create_label(
    #                                               owner=forgejo_safe_project_owner_name,
    #                                               repo=forgejo_safe_project_name,
    #                                               name=label.name, color=label.color,
    #                                               description=label.description)
    #                 fg_print.info(f"Label {label.name} imported")
    #             except ConflictError:
    #                 continue # already exists :-)
    #             except Exception as e:
    #                 detail = self.migration_dest._get_exception_detail(e)
    #                 fg_print.error(
    #                     f"Label {label.name} import failed: {detail}",
    #                     f"Failed to import label {label.name} for project "
    #                     f"{forgejo_safe_project_name} in Forgejo: {detail}",
    #                 )
    #                 continue



    # def _import_project_milestones(
    #     migration_dest: ForgejoDestination,
    #     milestones: list[gitlab.v4.objects.ProjectMilestone],
    #     project_owner: str,
    #     project_name: str,
    # ):
    #     """import milestones for a repository from a gitlab project"""
    #     forgejo_safe_project_name = name_clean(project_name)
    #     forgejo_safe_project_owner_name = name_clean(project_owner)
    #     try:
    #       forgejo_milestones = list(self.migration_dest.iter_forgejo_milestones(
    #                                                   owner=forgejo_safe_project_owner_name,
    #                                                   repo=forgejo_safe_project_name))
    #     except IterativeFetchError:
    #       pass
    #     for milestone in milestones:
    #         # Note: forgejo_add_milestone appends to the cached list of
    #         #       forgejo_milestones too for efficiency.
    #         success = self.migration_dest.forgejo_add_milestone(
    #                            owner=forgejo_safe_project_owner_name,
    #                            repo=forgejo_safe_project_name,
    #                            forgejo_milestones=forgejo_milestones,
    #                            title=milestone.title,
    #                            description=milestone.description,
    #                            due_date=milestone.due_date,
    #                            state=milestone.state)
    #         if not success:
    #             continue



    # def _import_project_issues(
    #     migration_dest: ForgejoDestination,
    #     issues: list[gitlab.v4.objects.ProjectIssue],
    #     project_owner: str,
    #     project_name: str,
    # ):
    #     """Import issues for a repo from a gitlab project"""
    #     forgejo_safe_project_owner = name_clean(project_owner)
    #     forgejo_safe_project_name = name_clean(project_name)

    #     # reload all existing milestones and labels, needed for assignment in issues
    #     try:
    #       forgejo_milestones = list(self.migration_dest.iter_forgejo_milestones(
    #                                 owner=forgejo_safe_project_owner,
    #                                 repo=forgejo_safe_project_name))
    #       forgejo_labels = list(self.migration_dest.iter_forgejo_labels(
    #                                 owner=forgejo_safe_project_owner,
    #                                 repo=forgejo_safe_project_name))
    #       forgejo_issues = list(self.migration_dest.iter_forgejo_issues(
    #                                 owner=forgejo_safe_project_owner,
    #                                 repo=forgejo_safe_project_name))
    #     except IterativeFetchError:
    #       pass
    #     for issue in issues:
    #         if not self.migration_dest.forgejo_issue_exists(forgejo_issues,
    #                                repo=forgejo_safe_project_name, issue_title=issue.title):
    #             due_date = ""
    #             if issue.due_date is not None:
    #                 due_date = dateutil.parser.parse(issue.due_date).strftime(
    #                     "%Y-%m-%dT%H:%M:%SZ"
    #                 )

    #             # extract assignee, mapping to forgejo safe username
    #             assignee = None
    #             if issue.assignee is not None:
    #                 assignee = name_clean(issue.assignee["username"])

    #             # extract list of assignees, mapping to forgejo safe username
    #             assignees : list[str] = []
    #             for tmp_assignee in issue.assignees:
    #                 assignees.append(name_clean(tmp_assignee["username"]))

    #             # Get milestone id for the issue, if milestone is assigned to the issue in GitLab.
    #             # # We need to get the milestone id for the milestone title from Forgejo, because
    #             # the milestone id in GitLab is not the same as the milestone id in Forgejo,
    #             # and we need the milestone id for the assignment of the milestone to the issue
    #             # in Forgejo.
    #             # If there is no milestone with the same title in Forgejo, we do not assign a
    #             # milestone to the issue in Forgejo, because there is no equivalent milestone in
    #             # Forgejo.
    #             forgejo_milestoneId = None
    #             missing_milestone = False
    #             if issue.milestone is not None:
    #                 forgejo_milestoneId = self.migration_dest.find_forgejo_milestone_id_by_title(
    #                           forgejo_milestones,
    #                           issue.milestone["title"]) # N.b. gitlab issue so dict
    #                 if forgejo_milestoneId is None:
    #                     # if this happens, something went wrong with the milestone import, because
    #                     # the milestone assigned to the issue in GitLab should have been imported
    #                     # to Forgejo in the milestone import step before the issue import step,
    #                     # so we print an error and skip the milestone assignment for this issue,
    #                     # but we continue with the import of the issue without the milestone
    #                     # assignment, because the existence of the milestone is not a failure
    #                     # for the import of the issue, we just skip the milestone assignment
    #                     # for this issue and continue with the import of the issue without the
    #                     # milestone assignment.
    #                     fg_print.error(
    #                         f"Milestone {issue.milestone['title']} assigned to "
    #                         f"issue {issue.title} does not exist in Forgejo, skipping milestone"
    #                         f" assignment for this issue",
    #                         f"Failed to import issue {issue.title} for project "
    #                         f"{forgejo_safe_project_name} in Forgejo",
    #                     )
    #                     missing_milestone = True
    #             if missing_milestone:
    #                 # stop the import of this issue (to allow milestone import to be fixed
    #                 # and re-run not to create duplicate issues)
    #                 continue


    #             missing_label = False
    #             forgejo_issue_label_ids : list[int] = []
    #             for label in issue.labels:
    #                 existing_label : Label = None
    #                 existing_label = next(
    #                     (item for item in forgejo_labels if item.name == label), None
    #                 )
    #                 if existing_label:
    #                     forgejo_issue_label_ids.append(existing_label.id)
    #                 else:
    #                     fg_print.error(
    #                         f"Label {label} assigned to issue {issue.title} does not exist"
    #                          " in Forgejo, skipping label assignment for this issue",
    #                         f"Failed to import issue {issue.title} for project {repo} in Forgejo",
    #                     )
    #                     missing_label = True
    #                     break
    #             if missing_label:
    #                 # stop the import of this issue (to allow milestone import to
    #                 # be fixed and re-run not to create duplicate issues)
    #                 continue

    #             try:
    #                 self.migration_dest.fg_api.issue.create_issue(
    #                                         owner=forgejo_safe_project_owner,
    #                                         repo=forgejo_safe_project_name,
    #                                         title=issue.title, body=issue.description,
    #                                         assignee=assignee, assignees=assignees,
    #                                         milestone=forgejo_milestoneId,
    #                                         labels=forgejo_issue_label_ids,
    #                                         due_on=due_date, closed=issue.state == "closed")
    #                 fg_print.info(f"Issue {issue.title} imported")
    #             except Exception as e:
    #                 detail = self.migration_dest._get_exception_detail(e)
    #                 fg_print.error(
    #                     f"Issue {issue.title} import failed: {detail}"
    #                     f"Failed to import issue {issue.title} for project"
    #                     f" {forgejo_safe_project_name} in Forgejo: {detail}",
    #                 )



    def _run_inbuilt_repo_import(self, source_repo: CanonicalRepo) -> bool:
        """Run the inbuilt import on the project.
           False if Forgejo is still migrating it in the background"""

        # get either the Forgejo User or Organization name as appropriate
        # for this gitlab project owner
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
                          " issues, labels, milestones, merge requests, releases)."
                          " This runs on the Forgejo server and can take several minutes,"
                          " please wait...")
            migrate_start = time.time()

            try:
                imported_repo : Repository | None = self.migration_dest.repo_migrate(
                                        source_repo=source_repo,
                                        forgejo_owner=forgejo_owner,
                                        service="gitlab",
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
            # NOTE: we set the must_change_password to False if we need to upload an avatar
            has_avatar = user.avatar_url is not None
            is_in_forgejo = self.migration_dest.forgejo_add_user(
                                                        user=user,
                                                        notify=notify,
                                                        must_change_password=not has_avatar)
            if not is_in_forgejo:
                # something went wrong with the user import. can't do any more for this user.
                continue

            if has_avatar:
                if user.password is None:
                    # Password will be none in event the user already exists. We can't help that.
                    fg_print.error(f"Unable to import avatar for user {user.username}"
                                   f" as this is only possible for newly created users")
                    continue

                try:
                    session = self.fg_api_builder.build_session(user.username, user.password)
                    avatar_b64 = self._image_url_to_base64(user.avatar_url)
                    url = f"{self.fg_api_builder.config.FORGEJO_API_URL}/user/avatar"
                    response = session.post(url = url, json={"image": avatar_b64})
                    response.raise_for_status()
                    fg_print.info(f"Imported avatar for user {user.username}")
                    session.close()
                    #NOTE: now update force user to change password on next login
                    self.migration_dest.forgejo_force_password_change(user=user)

                except requests.RequestException as ex:
                    fg_print.error(
                        f"Unable to import avatar for user {user.username}: {ex}"
                    )

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



    def _image_url_to_base64(self, url) -> str|None:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            return base64.b64encode(response.content).decode("utf-8")

        except requests.RequestException:
            return None



    def _image_url_to_data_url(self, url) -> str|None:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "image/jpeg")
            b64 = base64.b64encode(response.content).decode("utf-8")

            return f"data:{content_type};base64,{b64}"

        except requests.RequestException:
            return None


    def import_organizations(self):
        """import all organizations and their members"""
        fg_print.section("Groups / Organizations")
        fg_print.info(f"Collecting groups from {self.migration_source.get_source_system_name()},"
                      " this can take a while...")
        # read all users
        canonical_organizations: CanonicalOrganizations = self.migration_source.list_organizations()

        fg_print.info(f"Found {len(canonical_organizations.members)} "
                      f"{self.migration_source.get_source_system_name()}"
                      f" {canonical_organizations.source_type}")

        group_names = [org.username for org in canonical_organizations.members]
        fg_print.info(f"Importing groups... {group_names}")


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

            # Finally, import those group members
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

                # Handled by inbuilt repo migration
                # import labels
                #labels: list[gitlab.v4.objects.ProjectLabel] = project.labels.list(get_all=True)
                #fg_print.info(f"Found {len(labels)} labels for project {project.name}")
                #_import_project_labels(fg_api, labels, project_owner_name, project.name)

                # Handled by inbuilt repo migration
                # import milestones
                #milestones: list[gitlab.v4.objects.ProjectMilestone] = project.milestones.list(
                #                                                           all=True)
                #fg_print.info(f"Found {len(milestones)} milestones for project {project.name}")
                #_import_project_milestones(fg_api, milestones, project_owner_name, project.name)

                # Handled by inbuilt repo migration
                # import issues
                #issues: list[gitlab.v4.objects.ProjectIssue] = project.issues.list(get_all=True)
                #fg_print.info(f"Found {len(issues)} issues for project {project.name}")
                #_import_project_issues(fg_api, issues, project_owner_name, project.name)
            else:
                #fg_print.info(f"Skipping import of {source_repo.source_type} {source_repo.name}"
                #              f" from owner {source_repo.owner_name} *Contents*")
                pass
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
