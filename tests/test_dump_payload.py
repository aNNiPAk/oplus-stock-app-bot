import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from payload_dumper.source import FileSource

spec = importlib.util.spec_from_file_location("dump_payload", Path(__file__).resolve().parents[1] / "scripts/dump_payload.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class SignedURLTests(unittest.TestCase):
    def test_modern_manifest_preserves_offsets_above_four_gib(self):
        import struct
        from payload_dumper.core import parse_payload

        manifest_type = adapter.fix_manifest_schema()
        manifest = manifest_type()
        manifest.minor_version = 9
        partition = manifest.partitions.add()
        partition.partition_name = "my_product"
        operation = partition.operations.add()
        operation.type = 0
        operation.data_offset = 5 * 1024 ** 3 + 123
        operation.data_length = 6 * 1024 ** 3 + 456
        serialized = manifest.SerializeToString()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"CrAU" + struct.pack(">QQI", 2, len(serialized), 0) + serialized)
            source = FileSource(str(path))
            try:
                parsed = parse_payload(source).manifest.partitions[0].operations[0]
                self.assertEqual(parsed.data_offset, operation.data_offset)
                self.assertEqual(parsed.data_length, operation.data_length)
            finally:
                source.close()

    def test_signed_zip_is_unwrapped_without_changing_url(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "ota.zip"
            payload = b"CrAU" + b"payload data"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as output:
                output.writestr("payload.bin", payload)
            signed_url = "https://example.org/ota.zip?sign=test&t=123"
            with patch.object(adapter, "RangeHttpSource", return_value=FileSource(str(archive))) as source_factory:
                source = adapter.open_ota_source(signed_url)
                try:
                    self.assertEqual(source.read_at(0, len(payload)), payload)
                    self.assertEqual(source.size(), len(payload))
                    source_factory.assert_called_once_with(signed_url)
                finally:
                    source.close()

    def test_direct_payload_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "payload.bin"
            path.write_bytes(b"CrAUdirect payload")
            with patch.object(adapter, "RangeHttpSource", return_value=FileSource(str(path))):
                source = adapter.open_ota_source("https://example.org/download?sign=test")
                try:
                    self.assertEqual(source.read_at(0, 4), b"CrAU")
                finally:
                    source.close()

    def test_content_range_size_overrides_slice_content_length(self):
        import httpx
        data = b"CrAU" + bytes(range(100))

        def respond(request):
            start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
            return httpx.Response(206, headers={"Content-Range": f"bytes {start}-{end}/{len(data)}"}, content=data[start:end + 1])

        client = httpx.Client(transport=httpx.MockTransport(respond))
        source = adapter.RangeHttpSource("https://example.org/ota.zip?sign=test", client=client)
        try:
            self.assertEqual(source.size(), len(data))
            self.assertEqual(source.read_at(80, 10), data[80:90])
        finally:
            source.close()

    def test_server_ignoring_range_is_rejected(self):
        import httpx
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"entire archive")))
        with self.assertRaisesRegex(Exception, "ignored HTTP Range"):
            adapter.RangeHttpSource("https://example.org/ota.zip", client=client)
