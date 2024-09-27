# Migrate Salt Extensions

A tool that aids in the migration of sets of Salt modules out of Salt core into a Salt extension.

Please see the [salt-extension-copier docs](https://salt-extensions.github.io/salt-extension-copier/) for details, especially
the [extraction guide](https://salt-extensions.github.io/salt-extension-copier/topics/extraction.html).

## Usage
```console
usage: saltext-migrate [-h] [-m [MATCH ...]] [-i INCLUDE] [-e EXCLUDE] [--avoid-collisions] [-d DATA_FILE] [-y]
                       saltext_name

Migrate modules out of Salt core into an extension.

positional arguments:
  saltext_name          The name of the Salt extension to create (without `saltext` prefix!). Example: vault

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
  -d DATA_FILE, --data-file DATA_FILE
                        A YAML file providing defaults for Copier template questions. Handy when migrating many
                        modules. For available questions, see https://salt-extensions.github.io/salt-extension-
                        copier/ref/questions.html
  -y, --yes             Assume yes on all questions. Makes the migration non-interactive. In case you did not provide a
                        data-file with custom default answers, you need to update some answers to the Copier template
                        afterwards (especially author metadata)
```
