"""Check a fetch_apache.py download against the EKG schema: the raw JSON as-is, then through the adapters.

Kept for its name; the work is done by the adapter pipeline (ekg_adapters/). This is
build_graph.py with --raw-check and --no-write: nothing is written except an optional
JSON report. Exit code 1 if anything the adapters produce fails validation.

    python -I ekg-eval-data/check_schema.py
    python -I ekg-eval-data/check_schema.py --data ekg-eval-raw/apache-flink --json report.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_graph  # noqa: E402

if __name__ == "__main__":
    sys.argv[1:1] = ["--raw-check", "--no-write"]
    sys.exit(build_graph.main())
