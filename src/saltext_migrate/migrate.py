import fnmatch
import os
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import copier
import questionary
from plumbum import local
from plumbum.commands.processes import CommandNotFound, ProcessExecutionError

from .rewrite import (
    DunderUtilsMigrationResult,
    rewrite_module_imports,
    rewrite_patch_arglist,
    rewrite_tests_support_imports,
    rewrite_utils,
)

RECOMMENDED_PYVER = "3.10"


class TargetPathExists(ValueError):
    """
    Raised when renaming a path causes a conflict
    """


def ask_yn(msg, default=False):
    return questionary.confirm(msg, default=default).ask()


def render_list(iterator, list_style="•", indent=2):
    join = "\n" + " " * indent + list_style + " "
    return join[1:] + join.join(str(item) for item in iterator)


def render_dict_list(mapping, list_style_1="=>", list_style_2="•", indent=2):
    res = ""
    for key in sorted(mapping):
        res += "\n" + " " * indent + list_style_1 + f" {key}:\n"
        for val in sorted(mapping[key]):
            res += " " * (indent + 2) + list_style_2 + f" {val}\n"
    return res


def status(msg):
    questionary.print(f"\n    → {msg}", style="bold fg:darkgreen")


def warn(header, message=None):
    questionary.print(f"\n{header}", style="bold bg:darkred")
    if message:
        questionary.print(message)


def info(msg):
    questionary.print(msg, style="bold fg:ansiyellow")


def summary(msg, warn=False, title=False, main_title=False):
    style = "fg:"
    if main_title:
        questionary.print("\n\n")
        msg = "=" * 12 + f"  {msg}  " + "=" * 12
        style = "bold bg:"
    elif title:
        questionary.print("\n")
        msg = "-" * 12 + f"  {msg}  " + "-" * 12
        style = "bold bg:"
    if warn:
        style += "darkred bold"
    elif title or main_title:
        style += "ansiyellow fg:black"
    else:
        style = ""
    questionary.print(msg, style=style)


@dataclass
class Migration:
    result: list[Path]
    saltext_name: str
    saltext_path: Path
    avoid_collisions: bool = False
    renames: dict[Path, Path] = field(init=False)
    conflicts: dict[Path, Path] = field(init=False)
    dunder_utils_res: DunderUtilsMigrationResult = field(
        default_factory=DunderUtilsMigrationResult
    )
    failing_hooks: dict[str, str] = field(init=False)

    def __post_init__(self):
        self.renames = {}
        self.conflicts = {}
        self.utils_dunder_missed = defaultdict(set)
        self.utils_dunder_rewrite = defaultdict(set)
        self.failing_hooks = {}

        # rename salt/modules/foo.py => src/saltext/foo/modules/foo.py
        for path in self.modules:
            if path.parts[1] == "cloud":
                # cloud modules are in salt/cloud/clouds
                new_path = Path("src", "saltext", self.saltext_name, *path.parts[2:])
            elif path.parts[1:3] == ("client", "ssh", "wrapper"):
                # wrapper modules are in salt/client/ssh/wrapper
                new_path = Path("src", "saltext", self.saltext_name, *path.parts[3:])
            else:
                new_path = Path("src", "saltext", self.saltext_name, *path.parts[1:])

            self._rename(path, new_path)

        # remove `pytest` subdirectory
        # eg tests/pytests/unit/modules/test_foo.py => tests/unit/modules/test_foo.py
        for path in self.pytests:
            if path.parts[3] == "cloud":
                # cloud tests are in tests/pytests/{unit,integration}/cloud/clouds,
                # additionally drop `cloud`
                new_path = Path("tests", path.parts[2], *path.parts[4:])
            elif path.parts[:4] == ("tests", "pytests", "integration", "ssh"):
                # wrapper tests are in tests/pytests/integration/ssh,
                # additionally rename `ssh` -> `wrapper`
                new_path = Path("tests", *path.parts[2:-1], "wrapper")
            else:
                new_path = Path("tests", *path.parts[2:])
            self._rename_potentially_colliding_test(path, new_path)

        # non-pytest cloud tests are in tests/{unit,integration}/cloud/clouds,
        # drop `cloud`
        for path in self.non_pytests:
            if path.parts[2] != "cloud":
                continue
            self._rename_potentially_colliding_test(
                path, Path("tests", path.parts[1], *path.parts[3:])
            )

        # rename tests/support/pytest/mysql.py => tests/support/mysql.py
        for path in self.pytest_support:
            self._rename(path, Path("tests", "support", *path.parts[3:]))

        # rename doc/topics/foo.rst => docs/topics/foo.rst
        for path in self.doc:
            self._rename(path, Path("docs", *path.parts[1:]))

    def _rename(self, old, new) -> None:
        if new in self.result and new.exists() and new not in self.renames:
            raise TargetPathExists(new)
        if old == new:
            raise ValueError(f"This does not rename, {old} == {new}")
        self.renames[old] = new

    def _rename_potentially_colliding_test(self, old, new) -> None:
        avoid_collisions = self.avoid_collisions
        if not avoid_collisions:
            try:
                self._rename(old, new)
            except TargetPathExists:
                # This means there are still non-pytest tests at the same path
                # we want to move some (potentially historic) pytest ones to.
                # We cannot detect historic collisions reliably (those when
                # both files existed together and were touched by the same commit),
                # even filter-branch does not always detect it for some reason.
                # Example mysql: tests/integration/modules/test_mysql.py still exists,
                # tests/pytests/integration/modules/test_mysql.py existed at some point,
                # but was converted to functional tests.
                # A black update (6abb43d2dfc362643989ed9a856ae38cf9d4c61e) touched
                # both paths.
                self.conflicts[old] = new
                avoid_collisions = True
        if avoid_collisions:
            self._rename(new, new.with_stem(new.stem + "_old"))
            self._rename(old, new.with_stem(new.stem + "_pytest"))

    @cached_property
    def pytests(self) -> set[Path]:
        return set(filter(lambda x: x.parts[:2] == ("tests", "pytests"), self.result))

    @cached_property
    def pytest_support(self) -> set[Path]:
        return set(
            filter(lambda x: x.parts[:3] == ("tests", "support", "pytest"), self.result)
        )

    @cached_property
    def non_pytests(self) -> set[Path]:
        return set(
            filter(
                lambda x: x.suffix == ".py"
                and x.parts[0] == "tests"
                and x.parts[1] in ("unit", "integration"),
                self.result,
            )
        )

    @cached_property
    def modules(self) -> set[Path]:
        return set(filter(lambda x: x.parts[0] == "salt", self.result))

    @cached_property
    def module_types(self) -> set[str]:
        res = set()
        for mod in self.modules:
            res.add(mod.parts[1].rstrip("s"))
        return res

    @cached_property
    def module_imports(self) -> dict[str, str]:
        res = {}
        for mod in self.modules:
            # Example: res["salt.modules.mysql"] = "saltext.mysql.modules.mysql"
            res[".".join(mod.with_suffix("").parts)] = ".".join(
                self.renames[mod].with_suffix("").parts[1:]
            )
        return res

    @cached_property
    def test_files(self) -> set[Path]:
        return set(filter(lambda x: x.parts[0] == "tests", self.result))

    @cached_property
    def doc(self) -> set[Path]:
        return set(filter(lambda x: x.parts[0] == "doc", self.result))

    @property
    def args(self) -> tuple[str, ...]:
        res: list[str] = []
        for path in self.result:
            res.extend(("--path", str(path)))
            if path in self.renames:
                res.extend(("--path-rename", f"{path}:{self.renames[path]}"))
        return tuple(res)

    @property
    def non_pytests_after_migration(self) -> set[Path]:
        res = set()
        for path in self.non_pytests:
            if path in self.renames:
                path = self.renames[path]
            skip = True
            for old_path, new_path in self.renames.items():
                if new_path == path and old_path in self.pytests:
                    break
            else:
                skip = False
            if skip:
                continue

            if (self.saltext_path / path).exists():
                res.add(path)
        return res


git = local["git"]["-c", "commit.gpgsign=0"]
grep = local["grep"]
awk = local["awk"]
sort = local["sort"]
uniq = local["uniq"]


@dataclass
class ExtensionMigrate:
    saltext_name: str
    match: list[str] = field(default_factory=list)
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    avoid_collisions: bool = False
    non_interactive: bool = False
    salt_path: Path = field(init=False)
    saltext_path: Path = field(init=False)
    _copier_data: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self):
        self._ensure_cwd()
        self.salt_path = Path("salt").absolute()
        self.saltext_path = Path(f"saltext-{self.saltext_name}").absolute()
        self._copier_data = {
            "no_saltext_namespace": False,
            "license": "apache",
        }
        if self.non_interactive:
            self._copier_data["author"] = "Foo Bar"
            self._copier_data["author_email"] = "foo@b.ar"

    def execute(self):
        self._init_paths()
        res = self._filter()

        with local.cwd(self.salt_path):
            try:
                grep(
                    "salt_factories.get_container",
                    *list(filter(lambda x: x.exists(), res.test_files)),
                )
                self._copier_data["test_containers"] = True
            except ProcessExecutionError:
                pass

        self._execute_filter(res)
        self._copier_copy(res)
        self._merge_filtered()
        self._create_venv()
        self._rewrite_module_imports(res)
        self._rewrite_tests_support_imports(res)
        self._rewrite_patch_arglist(res)
        self._rewrite_utils(res)
        self._run_pre_commit(res)
        self._print_summary(res)
        self._cleanup()

    def _ensure_cwd(self):
        if Path(".").absolute().name == "salt":
            if Path(".git").exists():
                os.chdir("..")
            else:
                os.chdir("../../")

    def _init_paths(self):
        status(
            f"Initializing migration paths (Salt checkout, saltext-{self.saltext_name} dir)"
        )
        if not self.salt_path.exists():
            status("Did not find Salt checkout, cloning")
            git("clone", "https://github.com/saltstack/salt", "--single-branch")
        elif not self.salt_path.is_dir():
            raise RuntimeError(
                f"The path {self.salt_path} exists, but is not a directory"
            )
        elif not (self.salt_path / ".git").is_dir():
            raise RuntimeError(
                f"The path {self.salt_path} exists, but is not a git repository"
            )
        else:
            with local.cwd(self.salt_path):
                status("Removing potentially existing `filter-source` branch")
                # in case we're on a filter-source branch
                git("reset", "--hard", "master")
                git("switch", "master")
                try:
                    git("branch", "-D", "filter-source")
                except ProcessExecutionError:
                    pass

        if self.saltext_path.exists() and tuple(self.saltext_path.glob("*")):
            if not self.non_interactive and not ask_yn(
                f"Saltext directory {self.saltext_path} exists, remove?"
            ):
                raise RuntimeError("Saltext directory already exists and is not empty")
            shutil.rmtree(self.saltext_path)

        self.saltext_path.mkdir(exist_ok=True)

    def _filter(self) -> Migration:
        with local.cwd(self.salt_path):
            if not (self.salt_path / "rfcs" / "0004-dunder-runner.md").exists():
                raise ValueError(f"Salt checkout is invalid {self.salt_path}")

            status("Discovering related paths (historic and current)")

            git("switch", "master")
            try:
                git("branch", "-D", "filter-source")
            except ProcessExecutionError:
                pass

            if not Path(".git/filter-repo/analysis/path-all-sizes.txt").exists():
                status(
                    "Did not find existing `filter-repo --analyze` output. Renerating..."
                )
                git("filter-repo", "--analyze")
            git("switch", "-c", "filter-source")

            res: set[Path] = set()

            for single in self.match or [self.saltext_name]:
                cmd_chain = (
                    grep[
                        single,
                        ".git/filter-repo/analysis/path-all-sizes.txt",
                        ".git/filter-repo/analysis/path-deleted-sizes.txt",
                    ]
                    | awk["{print $5}"]
                    | sort
                    | uniq
                    | grep[
                        "-vE",
                        "^(.github|doc/ref|debian/|doc/locale|doc/_themes|salt/([^/]+/)?__init__.py|tests/(pytests/)?(unit|functional|integration)/conftest.py)",
                    ]
                )
                res = res.union(map(Path, cmd_chain().splitlines()))

            if self.include:
                for src in (
                    Path(".git/filter-repo/analysis/path-all-sizes.txt"),
                    Path(".git/filter-repo/analysis/path-deleted-sizes.txt"),
                ):
                    for line in src.read_text().splitlines()[2:]:
                        if any(
                            fnmatch.fnmatch(
                                src_path := re.split(r"\s+", line)[-1], ptrn
                            )
                            for ptrn in self.include
                        ):
                            res.add(Path(src_path))
            if self.exclude:
                for path in res.copy():
                    if any(fnmatch.fnmatch(str(path), ptrn) for ptrn in self.exclude):
                        res.remove(path)

            if not res:
                raise RuntimeError("Did not find any matching paths")

            if not self.non_interactive:
                selected = questionary.checkbox(
                    "Found the following paths. You can deselect any that you want ignored.",
                    choices=[
                        questionary.Choice(str(file), checked=True)
                        for file in sorted(res)
                    ],
                ).ask()
            else:
                selected = res
            if not selected:
                raise RuntimeError("Did not find any matching paths")
            selected = list(map(Path, selected))

            return Migration(
                selected,
                saltext_name=self.saltext_name,
                saltext_path=self.saltext_path,
                avoid_collisions=self.avoid_collisions,
            )

    def _execute_filter(self, res: Migration):
        status("Filtering repository history in new branch `filter-source`")

        with local.cwd(self.salt_path):
            git(
                "filter-repo",
                "--refs",
                "refs/heads/filter-source",
                "--force",
                *res.args,
            )
            try:
                git(
                    "rebase",
                    "--root",
                    "--empty=drop",
                    "--committer-date-is-author-date",
                )
            except ProcessExecutionError:
                git("rebase", "--abort")
            if not_deleted := list(self.salt_path.glob("**/*.py")):
                if not self.non_interactive and not ask_yn(
                    "Need to reset to before the great module purge."
                    "\n\nNote: Some files are still present in the Salt master branch. "
                    "Ensure they did not receive any updates after the purge PR.\n"
                    f"Files:\n{render_list(not_deleted, '*')}\n"
                ):
                    raise RuntimeError(
                        "Some files were not deleted during the great module purge, "
                        "not resetting to before to keep new changes. Files:\n"
                        + render_list(not_deleted, "*")
                    )
            git("reset", "--hard", "HEAD^{/Initial purge of community extensions}^")

    def _copier_copy(self, res: Migration):
        text = "Running copier"
        if not self.non_interactive:
            text += (
                ". Please answer the following questions. For help, see "
                "https://salt-extensions.github.io/salt-extension-copier/ref/questions.html"
            )
        status(text)

        with local.cwd(self.saltext_path):
            git("init", "--initial-branch", "main")
            copier_data = self._copier_data.copy()
            copier_data["project_name"] = self.saltext_name
            copier_data["loaders"] = list(
                sorted(res.module_types.difference(("util",)))
            )
            copier.run_copy(
                "https://github.com/salt-extensions/salt-extension-copier",
                unsafe=True,
                data=copier_data,
                defaults=self.non_interactive,
                quiet=True,
            )
            for glob in ("tests/**/test_*.py", "src/**/*_mod.py"):
                list(map(lambda x: x.unlink(), self.saltext_path.glob(glob)))

    def _merge_filtered(self):
        status("Merging filtered repository history")

        with local.cwd(self.saltext_path):
            git("remote", "add", "repo-source", self.salt_path)
            git("fetch", "repo-source")
            git("merge", "repo-source/filter-source")
            git("remote", "rm", "repo-source")
            for tag in git("tag").splitlines():
                git("tag", "-d", tag)

    def _rewrite_module_imports(self, res: Migration):
        status("Rewriting module imports")
        rewrite_module_imports(self.saltext_path, self.saltext_name, res)

    def _rewrite_tests_support_imports(self, res: Migration):
        status("Rewriting tests.support imports")
        rewrite_tests_support_imports(self.saltext_path, res)

    def _rewrite_patch_arglist(self, res: Migration):
        status("Rewriting unittest.mock.patch() arglist")
        rewrite_patch_arglist(self.saltext_path, res)

    def _rewrite_utils(self, res: Migration):
        status("Rewriting __utils__")
        res.dunder_utils_res = rewrite_utils(self.saltext_path, self.saltext_name, res)

        if res.dunder_utils_res.missed_critical:
            warn(
                "✗ Fix REQUIRED:",
                "The following Salt core utils mods require to be "
                "called via __utils__, which does not work from Saltext utils:\n"
                + render_dict_list(res.dunder_utils_res.missed_critical_mods),
            )
        if res.dunder_utils_res.rewrite_mods:
            warn(
                "✗ Fix REQUIRED:",
                "The following local utils mods required to be "
                "called via __utils__, which does not work for Saltext utils. "
                "Calls were rewritten partly, but you need to refactor the module "
                "to accept the required values and update the calls again:\n"
                + render_dict_list(res.dunder_utils_res.rewrite_mods),
            )
        if res.dunder_utils_res.missed:
            warn(
                "? Fix recommended:",
                "The following Salt core utils mods require to be "
                "called via __utils__, calls cannot be rewritten. Consider creating a PR:\n"
                + render_dict_list(res.dunder_utils_res.missed_mods),
            )

    def _create_venv(self):
        status(f"Creating virtual environment for saltext-{self.saltext_name}")

        with local.cwd(self.saltext_path):
            try:
                python = local[f"python{RECOMMENDED_PYVER}"]
            except CommandNotFound:
                python = local["python3"]
                version = python("--version").split(" ")[1]
                if (
                    not version.startswith(RECOMMENDED_PYVER)
                    and not self.non_interactive
                    and not ask_yn(
                        f"No `python{RECOMMENDED_PYVER}` executable found in $PATH. It is strongly "
                        f"recommended to use Python {RECOMMENDED_PYVER} for creating the virtual environment. "
                        f"Continue with `python3` (version {version}) anyways?"
                    )
                ):
                    raise RuntimeError(
                        f"No `python{RECOMMENDED_PYVER}` executable found in $PATH, exiting"
                    )
            python("-m", "venv", "venv", f"--prompt=saltext-{self.saltext_name}")
            self._run_in_venv("pip", "install", "-e", ".[dev,tests,docs]")
            self._run_in_venv("pre-commit", "install", "--install-hooks")

    def _run_pre_commit(self, res):
        def _run_pre_commit_loop(retries_left):
            try:
                self._run_in_venv("pre-commit", "run", "-a")
            except ProcessExecutionError:
                if retries_left > 0:
                    return _run_pre_commit_loop(retries_left - 1)
                raise

        status("Running pre-commit hooks against all files")

        try:
            _run_pre_commit_loop(2)
        except ProcessExecutionError as err:
            issues = re.split(r"\.{3,}Failed", err.stdout)
            hook_cnt = len(issues) - 1
            warn(f"Pre-commit is failing. Please fix all ({hook_cnt}) failing hooks")
            for cnt in range(hook_cnt):
                failing_hook = issues[cnt].splitlines()[-1]
                failing_output = "\n".join(issues[cnt + 1].splitlines()[:-1])
                warn(f"✗ Failing hook ({cnt + 1}): {failing_hook}", failing_output)
                res.failing_hooks[failing_hook] = failing_output

    def _run_in_venv(self, command, *args, **kwargs):
        with local.cwd(self.saltext_path):
            venv_bin_dir = Path("venv/bin").absolute()
            cmd = local[venv_bin_dir / command]
            with local.env(PATH=f"{venv_bin_dir}:{local.env['PATH']}"):
                cmd(*args, **kwargs)

    def _print_summary(self, res: Migration):
        next_steps: list[str] = [
            f"Change into the Saltext workdir: `cd saltext-{self.saltext_name}`",
            "Source the virtualenv: `source venv/bin/activate`",
        ]

        if "util" in res.module_types:
            next_steps.append(
                "Add the utils docs (`refs/utils/index`) to `docs/index.rst`"
            )

        summary("➨ Migration summary", main_title=True)

        summary("→ Migrated paths", title=True)
        for path in sorted(res.result):
            if path not in res.renames:
                text = f"  = {path} [Keep]"
                new_name = path
            elif path in res.conflicts:
                text = (
                    f"  x {path} [Rename (CONFLICT)] => {res.renames[path]} "
                    f"(conflicting: {res.conflicts[path]})"
                )
                new_name = res.renames[path]
            else:
                text = f"  ~ {path} [Rename] => {res.renames[path]}"
                new_name = res.renames[path]
            warn = False
            if (
                new_name in res.dunder_utils_res.missed_critical
                or new_name in res.dunder_utils_res.rewrite
            ):
                warn = True
                text += " (* Action required)"
            elif new_name in res.dunder_utils_res.missed:
                text += " (** Action recommended)"
            summary(text, warn=warn)

        if (
            res.dunder_utils_res.missed
            or res.dunder_utils_res.rewrite
            or res.failing_hooks
            or res.non_pytests_after_migration
        ):
            summary("✗ Outstanding issues to be resolved", title=True, warn=True)
            if res.dunder_utils_res.missed_critical:
                summary(
                    "\n  * Ensure the following Salt-internal utils modules don't "
                    "rely on global dunders and/or migrate them and change them locally:\n"
                    + render_list(res.dunder_utils_res.missed_mods, indent=4),
                    warn=True,
                )
                next_steps.append("Fix __utils__ dunder in utils")
            if res.dunder_utils_res.rewrite:
                summary(
                    "\n  * Rewrite the following migrated utils modules to not rely "
                    "on global dunders:\n"
                    + render_list(res.dunder_utils_res.rewrite, indent=4),
                    warn=True,
                )
                summary(
                    "\n  * Then ensure the following callers of the utils modules pass in "
                    "the required values:\n"
                    + render_list(res.dunder_utils_res.rewrite, indent=4),
                    warn=True,
                )
                next_steps.extend(
                    (
                        "Remove global dunders from utils modules",
                        "Update utils calls after removing dunders",
                    )
                )
            if res.failing_hooks:
                summary(
                    "\n  * Fix the following failing pre-commit hooks:\n"
                    + render_list(res.failing_hooks, indent=4),
                    warn=True,
                )
                next_steps.append("Fix pre-commit hooks")
            if res.non_pytests_after_migration:
                summary(
                    "\n  * Migrate the following non-pytest tests or skip them temporarily:\n"
                    + render_list(sorted(res.non_pytests_after_migration), indent=4),
                    warn=True,
                )
                next_steps.append("Migrate or skip non-pytests")

        next_steps += [
            "Ensure tests are passing: `nox -e tests-3`",
            "Ensure docs are building: `nox -e docs`",
            "Commit the repo: `git add . && git commit -m 'Initial extension layout'`",
            "Apply for a new repository in the `salt-extensions` org (optional)",
        ]
        summary(">> Next steps", title=True)
        summary(render_list(next_steps))

    def _cleanup(self):
        # cleanup after ourselves, but leave the salt checkout for future migrations
        with local.cwd(self.salt_path):
            git("switch", "master")
            try:
                git("branch", "-D", "filter-source")
            except ProcessExecutionError:
                pass
