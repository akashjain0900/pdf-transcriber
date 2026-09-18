#!/usr/bin/env python3
"""
Shim kept so the original command still works:

    python tools/import_legacy.py backup.json --apply

The implementation now lives in ledger/importer.py, inside the package, so it
is available in a bundled build too. Prefer:

    ledger import-legacy backup.json --apply
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger.cli import main

if __name__ == "__main__":
    print("Note: this is now `ledger import-legacy`. Running that.\n")
    sys.exit(main(["import-legacy"] + sys.argv[1:]))
