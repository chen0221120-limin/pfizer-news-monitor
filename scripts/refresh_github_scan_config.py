#!/usr/bin/env python3
"""Refresh the GitHub scan config bundle from the local Excel tracker."""

from __future__ import annotations

import base64
import gzip
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = ROOT / "scripts" / "build_gi_monitor_config_from_excel.py"
GENERATED_CONFIG = ROOT / "config" / "gi_monitor_config.generated.json"
BUNDLED_CONFIG = ROOT / "config" / "gi_monitor_config.generated.json.gz.b64"
SOURCE_XLSX = ROOT / "outputs" / "gi_competitors_extraction_updated.xlsx"


def main() -> int:
    if not SOURCE_XLSX.exists():
        print(f"Excel file not found: {SOURCE_XLSX}")
        return 1

    result = subprocess.run([sys.executable, str(BUILD_SCRIPT)], cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    data = json.loads(GENERATED_CONFIG.read_text(encoding="utf-8"))
    minified = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = base64.b64encode(gzip.compress(minified.encode("utf-8"), compresslevel=9)).decode("ascii")
    BUNDLED_CONFIG.write_text(payload, encoding="ascii")

    companies = len(data.get("companies", []))
    print("GitHub scan config refreshed successfully.")
    print(f"Excel source: {SOURCE_XLSX}")
    print(f"Generated config: {GENERATED_CONFIG}")
    print(f"GitHub bundle: {BUNDLED_CONFIG}")
    print(f"Companies included: {companies}")
    print("Next step: sync config/gi_monitor_config.generated.json.gz.b64 to GitHub.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
