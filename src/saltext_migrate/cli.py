import argparse
import sys

from .migrate import ExtensionMigrate


def main():
    """
    Prepare CLI args parser and hand off to DootiCLI
    """
    parser = argparse.ArgumentParser(
        prog="saltext-migrate",
        description="Migrate modules out of Salt core into an extension.",
    )
    parser.add_argument(
        "saltext_name",
        help=(
            "The name of the Salt extension to create (without `saltext` prefix!)."
            " Example: vault"
        ),
    )
    parser.add_argument(
        "-m",
        "--match",
        help=(
            "Instead of using the Salt extension name for finding paths, use this string. "
            "Can be specified multiple times"
        ),
        action="append",
        nargs="*",
    )
    parser.add_argument(
        "-i",
        "--include",
        help="Include these path globs in the migration. Can be specified multiple times.",
        action="append",
    )
    parser.add_argument(
        "-e",
        "--exclude",
        help="Exclude these path globs in the migration. Can be specified multiple times.",
        action="append",
    )
    parser.add_argument(
        "-b",
        "--base-branch",
        help=(
            "The Salt core branch the modules should be extracted from. "
            "Usually, the modules to migrate have been removed from `master` already "
            "and thus don't receive any updates there. If any fixes are merged, "
            "they end up in the `3006.x` and `3007.x` branches. "
            "This allows to specify the branch the module are extracted from. "
            "Defaults to `3007.x`."
        ),
    )
    parser.add_argument(
        "--purge-reset",
        help=(
            "When extracting modules from the `master` branch, reset the repository "
            "to one commit before the great module purge. This is necessary when "
            "extracting purged (!) modules from the `master` branch instead of the "
            "`3006.x` or `3007.x` ones. Ensure you have a good reason to do so."
        ),
        action="store_true",
    )
    parser.add_argument(
        "--avoid-collisions",
        help=(
            "When renaming paths, avoid collisions. This can be important when both "
            "pytests and non-pytests of the same type were present together at some "
            "point in Salt's history. Will result in the files names being suffixed "
            "with _old (non-pytest) and _pytest respectively"
        ),
        dest="avoid_collisions",
        action="store_true",
    )
    parser.add_argument(
        "-d",
        "--data-file",
        help=(
            "A YAML file providing defaults for Copier template questions. "
            "Handy when migrating many modules. For available questions, see "
            "https://salt-extensions.github.io/salt-extension-copier/ref/questions.html"
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        help=(
            "Assume yes on all questions. Makes the migration non-interactive. "
            "In case you did not provide a data-file with custom default answers, "
            "you need to update some answers to the Copier template afterwards "
            "(especially author metadata)"
        ),
        dest="non_interactive",
        action="store_true",
    )
    args = parser.parse_args()
    if len(sys.argv[1:]) == 0:
        parser.print_help()
        parser.exit()
    args = parser.parse_args()
    migration = ExtensionMigrate(
        saltext_name=args.saltext_name,
        match=args.match,
        include=args.include,
        exclude=args.exclude,
        avoid_collisions=args.avoid_collisions,
        non_interactive=args.non_interactive,
        data_file=args.data_file,
        base_branch=args.base_branch or "3007.x",
        purge_reset=args.purge_reset,
    )
    migration.execute()
