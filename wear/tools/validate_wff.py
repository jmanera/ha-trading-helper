"""Validate every watch face in wear/ against Google's official Watch Face Format XSD.

    uv run --no-project --with xmlschema python wear/tools/validate_wff.py <xsd-dir>

<xsd-dir> holds the unpacked wff-xsd.zip from https://github.com/google/watchface/releases
(one folder per format version). Each face is checked against the version its manifest
declares. The schemas are XSD 1.1, which xmllint does not support.

It also checks a rule the schema misses and Wear OS enforces when the face is applied: the
default provider types of a complication slot must be among its supportedTypes. A face that
breaks it installs fine but hangs the watch face picker.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import xmlschema

WEAR = Path(__file__).resolve().parents[1]


DEFAULT_TYPES = ("defaultSystemProviderType", "primaryProviderType", "secondaryProviderType")


def slot_errors(face: Path) -> list[str]:
    errors = []
    for slot in ET.parse(face).iter("ComplicationSlot"):  # noqa: S314 - our own files
        supported = set(slot.get("supportedTypes", "").split())
        policy = slot.find("DefaultProviderPolicy")
        for attr in DEFAULT_TYPES:
            value = policy.get(attr) if policy is not None else None
            if value and value not in supported:
                errors.append(
                    f"ComplicationSlot {slot.get('slotId')}: {attr}={value} "
                    f"is not in supportedTypes ({' '.join(sorted(supported))})"
                )
    return errors


def main() -> int:
    xsd_dir = Path(sys.argv[1])
    failed = 0
    faces = sorted(WEAR.glob("*/src/main/res/raw/watchface.xml"))
    for face in faces:
        manifest = (face.parents[2] / "AndroidManifest.xml").read_text()
        match = re.search(r'watchface\.format\.version"\s+android:value="(\d+)"', manifest)
        version = match.group(1) if match else "1"
        schema = xmlschema.XMLSchema11(str(xsd_dir / version / "watchface.xsd"))
        errors = [f"{e.path}: {e.reason}" for e in schema.iter_errors(str(face))]
        errors += slot_errors(face)
        name = face.relative_to(WEAR)
        print(f"{name} (v{version}): {'valid' if not errors else f'{len(errors)} errors'}")
        for error in errors[:20]:
            print(f"  - {error}")
        failed += bool(errors)
    if not faces:
        print("no watch faces found")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
