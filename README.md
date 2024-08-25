# Migrate Salt Extensions

A tool that aids in the migration of sets of Salt modules out of Salt core into a Salt extension.

Please see the [salt-extension-copier docs](https://salt-extensions.github.io/salt-extension-copier/) for details.

## Usage
```console
usage: saltext-migrate [-h] [-m [MATCH ...]] [-i INCLUDE] [-e EXCLUDE] [--avoid-collisions] [-y] saltext_name

Migrate modules out of Salt core into an extension.

positional arguments:
  saltext_name          The name of the Salt extension to create.

options:
  -h, --help            show this help message and exit
  -m [MATCH ...], --match [MATCH ...]
                        Instead of using the Salt extension name for finding paths, use this string. Can be specified
                        multiple times
  -i INCLUDE, --include INCLUDE
                        Include these path globs in the migration. Can be specified multiple times.
  -e EXCLUDE, --exclude EXCLUDE
                        Exclude these path globs in the migration. Can be specified multiple times.
  --avoid-collisions    When renaming paths, avoid collisions. This can be important when both pytests and non-pytests
                        of the same type were present together at some point in Salt's history. Will result in the
                        files names being suffixed with _old (non-pytest) and _pytest respectively
  -y, --yes             Assume yes on all questions. Makes the migration non-interactive. You need to update some
                        answers to the Copier template afterwards (especially author metadata)
```
