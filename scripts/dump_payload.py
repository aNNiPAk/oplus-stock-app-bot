#!/usr/bin/env python3
"""Compatibility adapter for signed OTA ZIP URLs in payload-dumper 2.3.0."""
from __future__ import annotations

import sys
import re
import threading
from collections import OrderedDict

from payload_dumper.source import ByteSource, SourceError


def fix_manifest_schema():
    """Modern OTA offsets are uint64; upstream 2.3.0 uses legacy uint32."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    from payload_dumper import update_metadata_pb2

    schema = descriptor_pb2.FileDescriptorProto()
    schema.ParseFromString(update_metadata_pb2.DESCRIPTOR.serialized_pb)
    operation = next(message for message in schema.message_type if message.name == "InstallOperation")
    for field in operation.field:
        if field.name in {"data_offset", "data_length"}:
            field.type = descriptor_pb2.FieldDescriptorProto.TYPE_UINT64
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(schema.SerializeToString())
    name = update_metadata_pb2.DeltaArchiveManifest.DESCRIPTOR.full_name
    manifest_type = message_factory.GetMessageClass(pool.FindMessageTypeByName(name))
    update_metadata_pb2.DeltaArchiveManifest = manifest_type
    return manifest_type


class RangeHttpSource(ByteSource):
    """Use Content-Range for size; Content-Length describes only the slice."""

    def __init__(self, url: str, *, client=None):
        import httpx

        self._url = url
        self._cache = OrderedDict()
        self._cache_lock = threading.Lock()
        self._block_size = 4 * 1024 * 1024
        self._requests = 0
        self._bytes = 0
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

    def _fetch(self, start: int, end: int) -> bytes:
        with self._client.stream("GET", self._url, headers={"Range": f"bytes={start}-{end}"}) as response:
            if self._check_range(response, start, end) != self._size:
                raise SourceError("OTA archive size changed while extracting")
            data = response.read()
        if len(data) != end - start + 1:
            raise SourceError("OTA server returned a truncated range")
        self._requests += 1
        self._bytes += len(data)
        return data

    def read_at(self, offset: int, length: int) -> bytes:
        if offset < 0:
            raise SourceError("Negative OTA offset")
        if length <= 0 or offset >= self._size:
            return b""
        end = min(offset + length, self._size)
        chunks = []
        # Bounded LRU combines neighboring operation reads; hashes are still
        # verified by payload-dumper. Lock preserves ByteSource thread safety.
        with self._cache_lock:
            while offset < end:
                start = offset // self._block_size * self._block_size
                data = self._cache.get(start)
                if data is None:
                    data = self._fetch(start, min(start + self._block_size, self._size) - 1)
                    self._cache[start] = data
                    if len(self._cache) > 8:
                        self._cache.popitem(last=False)
                self._cache.move_to_end(start)
                count = min(end - offset, len(data) - (offset - start))
                chunks.append(data[offset - start:offset - start + count])
                offset += count
        return b"".join(chunks)

    def size(self) -> int:
        return self._size

    def close(self) -> None:
        print(f"OTA reads: {self._requests} requests, {self._bytes} bytes; cache limit 32 MiB", flush=True)
        self._cache.clear()
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

    fix_manifest_schema()
    cli.open_source = open_ota_source
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())
