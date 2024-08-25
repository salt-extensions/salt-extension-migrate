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
        help="The name of the Salt extension to create.",
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
        "-y",
        "--yes",
        help=(
            "Assume yes on all questions. Makes the migration non-interactive. "
            "You need to update some answers to the Copier template afterwards "
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
    )
    migration.execute()
