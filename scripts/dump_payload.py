#!/usr/bin/env python3
"""Compatibility adapter for signed OTA ZIP URLs in payload-dumper 2.3.0."""
from __future__ import annotations

import sys
import re

from payload_dumper.source import ByteSource, SourceError


class RangeHttpSource(ByteSource):
    """Use Content-Range for size; Content-Length describes only the slice."""

    def __init__(self, url: str, *, client=None):
        import httpx

        self._url = url
        self._client = client or httpx.Client(
            follow_redirects=True,
            timeout=60,
            headers={
                "User-Agent": "Dalvik/2.1.0 (Linux; Android 16)",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache",
            },
        )
        try:
            with self._client.stream("GET", url, headers={"Range": "bytes=0-3"}) as response:
                self._size = self._check_range(response, 0, 3)
                if len(response.read()) != 4:
                    raise SourceError("OTA size probe returned a truncated range")
        except Exception:
            self._client.close()
            raise
        print(f"OTA transport: {self._size} bytes; HTTP Range verified", flush=True)

    @staticmethod
    def _check_range(response, start: int, end: int) -> int:
        response.raise_for_status()
        if response.status_code != 206:
            raise SourceError(f"OTA server ignored HTTP Range (HTTP {response.status_code})")
        content_range = response.headers.get("Content-Range", "")
        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
        if not match or (int(match[1]), int(match[2])) != (start, end):
            raise SourceError(f"OTA server returned wrong range: {content_range!r}; expected {start}-{end}")
        return int(match[3])

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self._size:
            return b""
        end = min(offset + length, self._size) - 1
        with self._client.stream("GET", self._url, headers={"Range": f"bytes={offset}-{end}"}) as response:
            if self._check_range(response, offset, end) != self._size:
                raise SourceError("OTA archive size changed while extracting")
            data = response.read()
        if len(data) != end - offset + 1:
            raise SourceError("OTA server returned a truncated range")
        return data

    def size(self) -> int:
        return self._size

    def close(self) -> None:
        self._client.close()


def open_ota_source(target: str):
    from payload_dumper.source import ZipMemberSource, open_source

    if not target.startswith(("https://", "http://")):
        return open_source(target)
    base = RangeHttpSource(target)
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
