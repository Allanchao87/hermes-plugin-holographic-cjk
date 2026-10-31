from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path("/Users/allan/.hermes/hermes-agent")
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Pytest treats a top-level __init__.py as a package file and may import it as
# ``__init__``. Load the hyphenated plugin under a valid package name used by
# these direct tests so relative imports behave like Hermes runtime discovery.
module_name = "holographic_zh"
spec = importlib.util.spec_from_file_location(
    module_name,
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
assert spec.loader is not None
spec.loader.exec_module(module)
