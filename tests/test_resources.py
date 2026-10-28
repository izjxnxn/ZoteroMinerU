"""Engine installation, integrity failures, cancellation and source packaging."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from zotero_mineru import resources
from zotero_mineru.translation import engine_command
from tools.package_release import source_files
from tools.audit_release import Audit


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cancelled = threading.Event()

    def archive(self, extra=None):
        path = self.home / "worker.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("BabelDocWorker/BabelDocWorker.exe", b"test-engine")
            zf.writestr("BabelDocWorker/_internal/python312.dll", b"test-runtime")
            for name, value in extra or []:
                info = zipfile.ZipInfo()
                info.filename = name
                zf.writestr(info, value)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_install_and_discover_without_changing_settings(self):
        archive, digest = self.archive()
        settings = self.home / "settings.json"
        settings.write_text('{"personal":"untouched"}', encoding="utf-8")
        installed = resources.unpack_worker(archive, self.home, digest, self.cancelled)
        with mock.patch.dict(os.environ, {"MINERU_ZOTERO_HOME": str(self.home)}):
            self.assertEqual(engine_command(), [str(installed)])
        self.assertEqual(installed.read_bytes(), b"test-engine")
        self.assertEqual(settings.read_text(encoding="utf-8"), '{"personal":"untouched"}')
        self.assertFalse(list(installed.parent.parent.parent.glob(".unpack-*")))

    def test_bad_hash_preserves_old_install(self):
        archive, _ = self.archive()
        old = resources.installed_worker(self.home)
        old.parent.mkdir(parents=True)
        old.write_bytes(b"old")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            resources.unpack_worker(archive, self.home, "0" * 64, self.cancelled)
        self.assertEqual(old.read_bytes(), b"old")

    def test_rejects_unsafe_paths_and_duplicate_windows_names(self):
        for name in ("../outside.txt", "BabelDocWorker/../../outside.txt", "C:/outside.txt",
                     "BabelDocWorker/evil\\path", "BabelDocWorker/babeldocworker.exe"):
            with self.subTest(name=name):
                archive, digest = self.archive([(name, b"bad")])
                with self.assertRaisesRegex(ValueError, "不安全"):
                    resources.unpack_worker(archive, self.home, digest, self.cancelled)
                self.assertFalse(resources.installed_worker(self.home).exists())

    def test_cancelled_install_is_never_discovered(self):
        archive, digest = self.archive()
        self.cancelled.set()
        with self.assertRaises(InterruptedError):
            resources.unpack_worker(archive, self.home, digest, self.cancelled)
        self.assertFalse(resources.installed_worker(self.home).exists())

    def test_missing_runtime_rejected(self):
        archive = self.home / "worker.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("BabelDocWorker/BabelDocWorker.exe", b"engine")
        with self.assertRaisesRegex(ValueError, "不完整"):
            resources.unpack_worker(archive, self.home, hashlib.sha256(archive.read_bytes()).hexdigest(), self.cancelled)

    def test_download_installs_and_removes_temporary_archive(self):
        archive, digest = self.archive()
        payload = archive.read_bytes()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(len(payload))}
        response.iter_content.return_value = [payload[:25], payload[25:]]
        progress = []
        with mock.patch.object(resources.requests, "get", return_value=response) as get, \
                mock.patch.object(resources, "WORKER_SHA256", digest):
            result = resources.download_worker(self.home, progress.append, self.cancelled)
        self.assertTrue(result.is_file())
        self.assertEqual(get.call_args.args[0], resources.WORKER_URL)
        self.assertTrue(any("校验" in event["text"] for event in progress))
        self.assertEqual([e["percent"] for e in progress if e["phase"] == "download"][-1], 100)
        install = [e["percent"] for e in progress if e["phase"] == "install"]
        self.assertTrue(install)
        self.assertEqual(install, sorted(install))
        self.assertEqual(install[-1], 99, "success is reported only after the atomic installation returns")
        self.assertFalse(list((self.home / "engines").glob(".download-*")))

    def test_source_allowlist_excludes_personal_data(self):
        (self.home / "main.py").write_text("# application", encoding="utf-8")
        for name in ("settings.json", "bridge.json", "private.md", "验收报告.md", "application.log"):
            (self.home / name).write_text("private", encoding="utf-8")
        (self.home / "zotero_mineru" / "__pycache__").mkdir(parents=True)
        (self.home / "zotero_mineru" / "__pycache__" / "x.py").write_text("private", encoding="utf-8")
        self.assertEqual([p.name for p in source_files(self.home)], ["main.py"])

    def test_credential_audit_detects_secrets_without_printing_them(self):
        secret = "private-fixture-value-12345"
        audit = Audit([secret])
        audit.scan("main.py", f"key={secret}".encode())
        audit.scan("settings.json", b"{}")
        self.assertEqual(len(audit.findings), 2)
        self.assertNotIn(secret, str(audit.findings))

    def test_public_certificates_and_spdx_names_are_not_private_keys(self):
        audit = Audit()
        audit.scan("certifi/cacert.pem", b"-----BEGIN CERTIFICATE-----\npublic certificate")
        audit.scan("licenses.py", b"sk-abcdefghijklmnopqrstuvwxyz-license")
        self.assertFalse(audit.findings)
        audit.scan("unexpected.txt", b"-----BEGIN RSA PRIVATE KEY-----\n" + b"A" * 64 + b"\n-----END RSA PRIVATE KEY-----")
        self.assertEqual(audit.findings[-1]["reason"], "private key found")


if __name__ == "__main__":
    unittest.main()
