import importlib
import os
import sys

_HERE = os.path.dirname(__file__)
LAYER_PATH = os.path.join(_HERE, "..", "layers", "common", "python")
FUNCTIONS_PATH = os.path.join(_HERE, "..", "functions")

sys.path.insert(0, LAYER_PATH)


def load_function_module(function_dir: str, name: str = "app"):
    """Import `name` from functions/<function_dir>/ fresh, the way Lambda's
    /var/task does. Both functions ship a module called `app`, so the wanted
    directory goes to the front of sys.path and any cached copy is dropped
    first."""
    path = os.path.join(FUNCTIONS_PATH, function_dir)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    for mod in list(sys.modules):
        if mod == name or mod.startswith(name + "."):
            del sys.modules[mod]
    return importlib.import_module(name)
