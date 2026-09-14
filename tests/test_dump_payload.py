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
    def test_signed_zip_is_unwrapped_without_changing_url(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "ota.zip"
            payload = b"CrAU" + b"payload data"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as output:
                output.writestr("payload.bin", payload)
            signed_url = "https://example.org/ota.zip?sign=test&t=123"
            with patch("payload_dumper.source.HttpSource", return_value=FileSource(str(archive))) as source_factory:
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
            with patch("payload_dumper.source.HttpSource", return_value=FileSource(str(path))):
                source = adapter.open_ota_source("https://example.org/download?sign=test")
                try:
                    self.assertEqual(source.read_at(0, 4), b"CrAU")
                finally:
                    source.close()
