"""
Rewrite code from Salt core to work in the context of a Salt extension.

This code is heavily based on salt-rewrite:
    * https://github.com/saltstack/salt-rewrite/blob/master/src/saltrewrite/salt_extensions/fix_saltext.py
    * https://github.com/saltstack/salt-rewrite/blob/master/src/saltrewrite/salt/fix_dunder_utils.py
"""

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from bowler import SYMBOL, TOKEN, Query
from bowler.types import Leaf, Node
from fissix.fixer_util import Call, Dot, touch_import

if TYPE_CHECKING:
    from .migrate import Migration


SALT_DUNDERS = (
    "__active_provider_name__",
    "__context__",
    "__env__",
    "__events__",
    "__executors__",
    "__grains__",
    "__instance_id__",
    "__jid_event__",
    "__low__",
    "__lowstate__",
    "__master_opts__",
    "__opts__",
    "__pillar__",
    "__proxy__",
    "__reg__",
    "__ret__",
    "__runner__",
    "__running__",
    "__salt__",
    "__salt_system_encoding__",
    "__serializers__",
    "__states__",
    "__utils__",
)


def rewrite_module_imports(saltext_path: Path, saltext_name: str, res: "Migration"):
    def _create_filter(mod, from_import=False):
        def _filter_salt_from_imports(node, capture, filename):
            match = (
                f"from salt.{'.'.join(mod.split('.')[:-1])} import {mod.split('.')[-1]}"
            )
            return match in str(capture["node"])

        def _filter_salt_imports(node, capture, filename):
            return mod in str(capture["node"])

        if from_import:
            return _filter_salt_from_imports
        return _filter_salt_imports

    query = Query([str(saltext_path / "src"), str(saltext_path / "tests")])
    for mod_path in res.modules:
        mod_parent = ".".join(mod_path.with_suffix("").parts[1:-1])
        mod = ".".join(mod_path.with_suffix("").parts[1:])
        for from_import in (False, True):
            if from_import:
                # Bowler does not recognize from imports otherwise
                query = query.select_module(f"salt.{mod_parent}")
            else:
                query = query.select_module(f"salt.{mod}")
            query = query.filter(_create_filter(mod, from_import))
            if from_import:
                query = query.rename(f"saltext.{saltext_name}.{mod_parent}")
            else:
                query = query.rename(f"saltext.{saltext_name}.{mod}")
    query.execute(write=True, interactive=False, silent=False)


def rewrite_tests_support_imports(saltext_path: Path, res: "Migration"):
    query = Query([str(saltext_path / "tests")])
    query = query.select_module("tests.support.mock")
    query = query.rename("unittest.mock")

    # We're moving tests/support/pytest/mysql.py to tests/support/mysql.py.
    for mod_path in res.pytest_support:
        if mod_path not in res.renames:
            continue
        old_mod = ".".join(mod_path.with_suffix("").parts)
        new_mod = ".".join(res.renames[mod_path].with_suffix("").parts)
        query.select_root()
        query = query.select_module(old_mod)
        query = query.rename(new_mod)

    query.execute(write=True, interactive=False, silent=False)


def rewrite_patch_arglist(saltext_path: Path, res: "Migration"):
    def _filter_salt_imports(node, capture, filename):
        node = str(capture["node"])
        return any(match in node for match in res.module_imports)

    def _replace_patch_arglist(node, capture, filename):
        if hasattr(node, "children"):
            for child in node.children:
                if hasattr(child, "children") and child.children:
                    for _child in child.children:
                        if hasattr(_child, "children") and _child.children:
                            for __child in _child.children:
                                if hasattr(__child, "value"):
                                    try:
                                        old_import = next(
                                            x
                                            for x in res.module_imports
                                            if x in __child.value
                                        )
                                    except StopIteration:
                                        continue
                                    __child.value = __child.value.replace(
                                        old_import, res.module_imports[old_import]
                                    )
                        elif hasattr(_child, "value"):
                            try:
                                old_import = next(
                                    x for x in res.module_imports if x in _child.value
                                )
                            except StopIteration:
                                continue
                            _child.value = _child.value.replace(
                                old_import, res.module_imports[old_import]
                            )

    query = Query([str(saltext_path / "tests")])
    query = query.select_function("patch")
    query = query.filter(_filter_salt_imports)
    query = query.modify(_replace_patch_arglist)
    # Also replace in patch.dict.
    query = query.select_root()
    query = query.select_method("dict")
    query = query.filter(_filter_salt_imports)
    query = query.modify(_replace_patch_arglist)
    # patch.object is rewritten by rewrite_module_imports above
    query.execute(write=True, interactive=False, silent=False)


class DunderParser(ast.NodeTransformer):  # pylint: disable=missing-class-docstring
    # pylint: disable=missing-function-docstring,invalid-name
    def __init__(self):
        self.virtualname = None
        self.uses_salt_dunders = False

    def visit_Name(self, node):
        if node.id in SALT_DUNDERS:
            self.uses_salt_dunders = True
        return self.generic_visit(node)

    def visit_Assign(self, node):
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "__virtualname__":
                self.virtualname = node.value.s
        return self.generic_visit(node)

    # pylint: enable=missing-function-docstring,invalid-name


def _get_salt_code_root():
    return (next(Path(".venv/lib").glob("python3.*")) / "site-packages").resolve()


def _defaultdict_factory():
    return defaultdict(set)


@dataclass
class DunderUtilsMigrationResult:
    # __utils__ used to call Salt core utils depending on dunders from non-utils Saltext modules
    # This is non-critical until __utils__ is deprecated or the utils module
    # is extracted to a Saltext.
    _missed: dict[Path, set[str]] = field(default_factory=_defaultdict_factory)
    # __utils__ used to call Salt core utils depending on dunders from utils Saltext modules
    # This is very critical since Saltext utils are not loaded by the loader.
    _missed_critical: dict[Path, set[str]] = field(default_factory=_defaultdict_factory)
    # __utils__ used to call migrated utils depending on dunders.
    # The rewrite only performs a partial migration since the passing in of
    # new arguments requires refactoring.
    _rewrite: dict[Path, set[str]] = field(default_factory=_defaultdict_factory)

    @property
    def missed(self):
        return {k: v for k, v in self._missed.items() if v}

    @property
    def missed_mods(self):
        res = defaultdict(set)
        for path, mods in self._missed.items():
            for mod in mods:
                res[mod].add(path)
        return dict(res)

    @property
    def missed_critical(self):
        return {k: v for k, v in self._missed_critical.items() if v}

    @property
    def missed_critical_mods(self):
        res = defaultdict(set)
        for path, mods in self._missed_critical.items():
            for mod in mods:
                res[mod].add(path)
        return dict(res)

    @property
    def rewrite(self):
        return {k: v for k, v in self._rewrite.items() if v}

    @property
    def rewrite_mods(self):
        res = defaultdict(set)
        for path, mods in self._rewrite.items():
            for mod in mods:
                res[mod].add(path)
        return dict(res)


@dataclass
class UtilsMigrator:
    saltext_name: str
    saltext_path: Path
    res: "Migration"
    utils_info: dict[Path, dict] = field(init=False, repr=False)
    _salt_base_path: Path = field(init=False)
    _salt_utils_path: Path = field(init=False)
    _saltext_base_path: Path = field(init=False)
    _saltext_utils_path: Path = field(init=False)
    _utils_res: DunderUtilsMigrationResult = field(
        default_factory=DunderUtilsMigrationResult
    )

    def __post_init__(self):
        self._salt_base_path = (
            next((self.saltext_path / ".venv" / "lib").glob("python3.*"))
            / "site-packages"
        ).resolve()
        self._saltext_base_path = (self.saltext_path / "src").resolve()
        self._salt_utils_path = (self._salt_base_path / "salt" / "utils").resolve()
        self._saltext_utils_path = (
            self._saltext_base_path / "saltext" / self.saltext_name / "utils"
        )
        self.utils_info = self._get_utils_module_info()

    def _get_utils_module_info(self):
        """
        Collect utils modules dunder information.
        """
        mapping = {}
        for base_path, utils_path in (
            (self._salt_base_path, self._salt_utils_path),
            (self._saltext_base_path, self._saltext_utils_path),
        ):
            for path in utils_path.rglob("*.py"):
                transformer = DunderParser()
                tree = ast.parse(path.read_text())
                transformer.visit(tree)
                mapping[path.resolve()] = {
                    "modname": path.stem,
                    "virtualname": transformer.virtualname or path.stem,
                    "uses_salt_dunders": transformer.uses_salt_dunders,
                    "migrated": self._saltext_base_path in path.parents,
                    "import": ".".join(
                        path.relative_to(base_path).with_suffix("").parts
                    ),
                }
        return mapping

    def get_utils_module_details(self, name):
        """
        Return utils module details.
        """
        full_saltext_module_name = f"saltext.{self.saltext_name}.utils.{name}"
        full_module_name = f"salt.utils.{name}"
        for base_path, full_name in (
            (self._saltext_base_path, full_saltext_module_name),
            (self._salt_base_path, full_module_name),
        ):
            full_module_path = (
                base_path / Path(*full_module_name.split(".")).with_suffix(".py")
            ).resolve()
            if full_module_path.exists():
                return self.utils_info[full_module_path]
            modname = name.split(".")[0]
            for modpath, entry in self.utils_info.items():
                if base_path in modpath.parents and entry["virtualname"] == modname:
                    return entry
        raise RuntimeError(
            f"Could not find the python module for {name!r} and '{full_module_path}' does not exist"
        )

    def fix_dunder_utils_calls(self, node, capture, filename):
        """
        Automatically rewrite dunder utils calls to call the module directly.
        """
        if "dunder_mod_func" not in capture:
            return
        dunder_mod_func = capture["dunder_mod_func"][0].value.strip("'").strip('"')

        utils_module, utils_module_funcname = dunder_mod_func.split(".")
        details = self.get_utils_module_details(utils_module)
        if details["uses_salt_dunders"]:
            if not details["migrated"]:
                tgt = "_missed"
                if (
                    Path(filename).relative_to(self._saltext_base_path).parts[2]
                    == "utils"
                ):
                    tgt = "_missed_critical"
                getattr(self._utils_res, tgt)[
                    Path(filename).relative_to(self.saltext_path)
                ].add(details["import"])
                return  # Don't rewrite this, we can't influence Salt core
            # Partially rewrite calls to migrated utils depending on global dunders
            self._utils_res._rewrite[Path(filename).relative_to(self.saltext_path)].add(
                details["import"]
            )

        # Make sure we import the right utils module
        touch_import(None, details["import"], node)

        # Un-parent the function arguments so we can add them to a new call
        for leaf in capture["function_arguments"]:
            leaf.parent = None

        # Un-parent any trailign code after the __utils__ call too
        for leaf in capture["trailing"]:
            leaf.parent = None

        # Create the new function call
        call_node = Call(
            Leaf(TOKEN.NAME, utils_module_funcname, prefix=""),
            capture["function_arguments"],
        )

        trailer = []
        parts = details["import"].split(".")
        for part in parts[1:]:
            trailer.extend((Dot(), Leaf(TOKEN.NAME, part, prefix="")))
        trailer.extend((Dot(), call_node))

        # Create replacement node
        replacement = Node(
            SYMBOL.power,
            [
                Leaf(TOKEN.NAME, parts[0], prefix=capture["node"].prefix),
                Node(
                    SYMBOL.trailer,
                    trailer,
                ),
                *capture["trailing"],
            ],
        )
        # Replace the whole node with the new function call
        node.replace(replacement)


def rewrite_utils(saltext_path: Path, saltext_name: str, res: "Migration"):
    """
    Rewrite the passed in paths
    """
    fixer = UtilsMigrator(saltext_name=saltext_name, saltext_path=saltext_path, res=res)
    (
        Query(saltext_path / "src")
        .select(
            """
            (
                dunder_call=power<
                    '__utils__'
                    trailer< '[' dunder_mod_func=any* ']' >
                    trailer< '(' function_arguments=any* ')' >
                    trailing=any*
                >
            )
            """
        )
        .modify(fixer.fix_dunder_utils_calls)
        .execute(write=True, interactive=False, silent=False)
    )
    return fixer._utils_res
