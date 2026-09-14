#!/usr/bin/env python3
"""Compatibility adapter for signed OTA ZIP URLs in payload-dumper 2.3.0."""
from __future__ import annotations

import sys


def open_ota_source(target: str):
    from payload_dumper.source import HttpSource, ZipMemberSource, open_source

    if not target.startswith(("https://", "http://")):
        return open_source(target)
    base = HttpSource(target)
    try:
        if base.read_at(0, 4) == b"PK\x03\x04":
            return ZipMemberSource(base)
        return base
    except Exception:
        base.close()
        raise


def main() -> int:
    from payload_dumper import cli

    cli.open_source = open_ota_source
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())
