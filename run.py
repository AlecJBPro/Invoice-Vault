"""Launch this checkout regardless of its directory name (no PYTHONPATH needed)."""
import importlib.util
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('invoices', root / '__init__.py',
                                                 submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules['invoices'] = package
    spec.loader.exec_module(package)
    from invoices.__main__ import main as cli
    cli()


if __name__ == '__main__':
    main()
