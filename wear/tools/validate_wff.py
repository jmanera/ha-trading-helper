"""Validate every watch face in wear/ against Google's official Watch Face Format XSD.

    uv run --no-project --with xmlschema python wear/tools/validate_wff.py <xsd-dir>

<xsd-dir> holds the unpacked wff-xsd.zip from https://github.com/google/watchface/releases
(one folder per format version). Each face is checked against the version its manifest
declares. The schemas are XSD 1.1, which xmllint does not support.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import xmlschema

WEAR = Path(__file__).resolve().parents[1]


def main() -> int:
    xsd_dir = Path(sys.argv[1])
    failed = 0
    faces = sorted(WEAR.glob("*/src/main/res/raw/watchface.xml"))
    for face in faces:
        manifest = (face.parents[2] / "AndroidManifest.xml").read_text()
        match = re.search(r'watchface\.format\.version"\s+android:value="(\d+)"', manifest)
        version = match.group(1) if match else "1"
        schema = xmlschema.XMLSchema11(str(xsd_dir / version / "watchface.xsd"))
        errors = list(schema.iter_errors(str(face)))
        name = face.relative_to(WEAR)
        print(f"{name} (v{version}): {'valid' if not errors else f'{len(errors)} errors'}")
        for error in errors[:20]:
            print(f"  - {error.path}: {error.reason}")
        failed += bool(errors)
    if not faces:
        print("no watch faces found")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
