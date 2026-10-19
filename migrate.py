#!/usr/bin/env python3
#
# imports users, organizations, repositories (with issues, merge / pull requests, wiki...),
# keys and collaborators from GitLab or GitHub to Forgejo
#
"""
Usage: migrate.py [--debug] [--users] [--groups] [--projects] [--membership] [--attachments] [--all] [--notify] [--refresh-cache] [--from-start]
       migrate.py --help

Migrates users, organizations, repositories, their members and attachments from GitLab or GitHub
(set in [source] of .migrate.ini) to Forgejo.

Options
  -h, --help    Show this screen
  --debug       show extra debug output
  --users       migrate users
  --groups      migrate organizations (GitLab groups, GitHub organizations)
  --projects    migrate repositories (GitLab projects, GitHub repositories)
  --membership  migrate repository members
  --attachments    copy files linked in issues, merge / pull requests and comments (e.g.
                   pasted images) into Forgejo and fix the links (migrated projects)
  --all         migrate all
  --notify      send notification to users
  --refresh-cache  ignore cached source data (users, organizations, repositories,
                   members) and fetch it again
  --from-start     go over all items of each step, instead of continuing at the
                   item where the last (interrupted) run of that step stopped
"""
import os
import time
import configparser

from docopt import docopt


from fg_migration.adapters.forgeo_types import ForgejoApiBuilder
from fg_migration.core.config_types import ForgejoConfig
from fg_migration.adapters.destination_forgjo import ForgejoDestination
from fg_migration.services.migrator import Migrator
from fg_migration.services.attachment_migrator import AttachmentMigrator
from fg_migration.core.caching_source import build_source
from fg_migration.core.json_cache import JsonCache

from fg_migration.utils import fg_print

SCRIPT_VERSION = "1.0.0"


def main() -> int:
    """Main function"""

    #######################
    # CONFIG SECTION START
    #######################
    if not os.path.exists(".migrate.ini"):
        fg_print.error("Please create .migrate.ini as explained in the README")
        return 1

    config = configparser.RawConfigParser()
    config.read(".migrate.ini")
    forgejo_config = ForgejoConfig.from_config(config=config)

    #######################
    # CONFIG SECTION END
    #######################

    args = docopt(__doc__)
    # control debug logging
    if args["--debug"]:
        fg_print.IS_DEBUG = True

    fg_print.print_color(
        fg_print.Bcolors.HEADER, "---=== Migration to Forgejo ===---"
    )
    fg_print.info(f"Version: {SCRIPT_VERSION}\n")
    start_time = time.time()


    try:
        migration_source = build_source(config=config,
                                        refresh_cache=bool(args["--refresh-cache"]))
    except ValueError as e:
        fg_print.error(str(e))
        return 1
    except ConnectionError:
        return 2

    fg_api_builder = ForgejoApiBuilder(forgejo_config=forgejo_config)
    fg_api = fg_api_builder.build_forgejo_api_client()
    fg_conn_success= fg_api_builder.test_forgejo_connection(fg_api=fg_api)

    if not fg_conn_success:
        return 1

    migration_dest : ForgejoDestination = ForgejoDestination(fg_api=fg_api,
                                                             forgejo_config=forgejo_config)
    migrator = Migrator(migration_source=migration_source,
                        migration_dest=migration_dest,
                        fg_api_builder=fg_api_builder)

    run_users = args["--users"] or args["--all"]
    run_groups = args["--groups"] or args["--all"]
    run_projects = args["--projects"] or args["--all"]
    run_membership = args["--membership"] or args["--all"]
    run_attachments = args["--attachments"] or args["--all"]

    # where each step stopped, so an interrupted step continues from there
    from_start = bool(args["--from-start"])
    checkpoint_cache = JsonCache(cache_dir=os.path.join(migration_source.cache.cache_dir,
                                                        "checkpoints"),
                                 max_age_hours=24 * 365,
                                 identity=migration_source.cache.identity,
                                 secrets={})
    migrator.checkpoint_cache = checkpoint_cache
    migrator.from_start = from_start

    # IMPORT NOTHING ?
    if not (run_users or run_groups or run_projects or run_membership or run_attachments):
        fg_print.info("")
        fg_print.warning("No migration option(s) selected, nothing to do")
        return 0

    try:
        # IMPORT System Users
        if run_users:
            notify=bool(args["--notify"])
            migrator.import_users(notify=notify)
        # IMPORT Organizations and Teams (with their member Users)
        if run_groups:
            migrator.import_organizations()
        # IMPORT Repositories (Projects) AND OR Collaborators (Memberships of Projects)
        if run_projects or run_membership:
            migrator.import_repos(import_repo_content=run_projects)
        if run_attachments:
            AttachmentMigrator(migration_source=migration_source, migration_dest=migration_dest,
                               done_cache=migration_source.cache,
                               checkpoint_cache=checkpoint_cache,
                               from_start=from_start).migrate_attachments()
    except Exception as e:
        fg_print.error(str(e))
        return 1
    finally:
        migrator.close()

    fg_print.info("")
    fg_print.info(f"Total time: {fg_print.format_duration(time.time() - start_time)}")
    if fg_print.GLOBAL_ERROR_COUNT == 0:
        fg_print.success("Migration finished with no errors")
    else:
        fg_print.error(f"Migration finished with {fg_print.GLOBAL_ERROR_COUNT} errors")
        fg_print.info("Failed elements:")
        print(*fg_print.GLOBAL_ERROR_LIST, sep="\n")
        return 1
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
