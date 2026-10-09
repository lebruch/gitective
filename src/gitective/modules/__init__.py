"""Module auto-discovery: any .py file in this folder defining NAME, HELP,
add_arguments() and run() becomes a 'gitective <NAME>' subcommand.
There is no registry to edit when adding a module.
"""

import importlib
import pkgutil


def discover() -> dict:
    """Import every module of this package and return {NAME: module} for the valid ones."""
    found = {}
    package = importlib.import_module(__name__)
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{__name__}.{module_name}")
        if hasattr(module, "NAME") and hasattr(module, "add_arguments") and hasattr(module, "run"):
            found[module.NAME] = module
    return found
