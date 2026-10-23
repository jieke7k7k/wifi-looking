"""ClashMi discovery and client tests with a stubbed requests layer.

Covers the review findings: JSON null settings turning into the string
"None", REG_BINARY registry values, the pre-count GET gating the
DELETE in close_connections, 401 rediscovery, and timeouts.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clashmi
import requests
from clashmi import (
    ClashMiClient,
    ClashMiError,
    ClashMiSettings,
    _derive_secret,
    _settings_from_file,
    _settings_from_registry,
)


DID = "AAAABBBBCCCCDDDDEEEEFFFFGGGG"
DID_SECRET = "CCCCDDDDEEEEFFFF"  # DID[8:24]


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def make_client() -> ClashMiClient:
    return ClashMiClient(
        ClashMiSettings(base_url="http://127.0.0.1:9090", secret="s3cret", source="test")
    )


class SettingsFromFileTests(unittest.TestCase):
    def write_settings(self, tmp: str, payload) -> Path:
        path = Path(tmp) / "service_core_setting.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_null_secret_becomes_empty_string(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_settings(
                tmp, {"external-controller": "127.0.0.1:9090", "secret": None}
            )
            settings = _settings_from_file(path)
            self.assertIsNotNone(settings)
            self.assertEqual(settings.secret, "")
            self.assertEqual(settings.base_url, "http://127.0.0.1:9090")

    def test_null_controller_is_missing_not_http_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_settings(
                tmp, {"external-controller": None, "secret": "abc"}
            )
            self.assertIsNone(_settings_from_file(path))

    def test_normal_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_settings(
                tmp, {"external-controller": "192.168.1.1:9090", "secret": "abc"}
            )
            settings = _settings_from_file(path)
            self.assertEqual(settings.base_url, "http://192.168.1.1:9090")
            self.assertEqual(settings.secret, "abc")


class DeriveSecretTests(unittest.TestCase):
    def test_derive_from_did(self) -> None:
        self.assertEqual(_derive_secret(DID), DID_SECRET)

    def test_derive_from_reg_binary_bytes(self) -> None:
        self.assertEqual(_derive_secret(DID.encode("utf-8")), DID_SECRET)

    def test_short_did_rejected(self) -> None:
        self.assertIsNone(_derive_secret("short"))
        self.assertIsNone(_derive_secret(None))

    def test_registry_settings_use_derived_secret(self) -> None:
        with mock.patch.object(clashmi, "_read_registry_did", return_value=DID):
            settings = _settings_from_registry()
        self.assertIsNotNone(settings)
        self.assertEqual(settings.secret, DID_SECRET)
        self.assertEqual(settings.source, "registry")


class DiscoveryOrderTests(unittest.TestCase):
    def tearDown(self) -> None:
        clashmi.invalidate_settings_cache()

    def test_fallback_when_nothing_found(self) -> None:
        with mock.patch.object(clashmi, "_settings_from_file", return_value=None), (
            mock.patch.object(clashmi, "_read_registry_did", return_value=None)
        ):
            settings = ClashMiClient.discover_settings(use_cache=False)
        self.assertEqual(settings.source, "fallback")


class CloseConnectionsTests(unittest.TestCase):
    def test_delete_sent_even_when_count_get_fails(self) -> None:
        client = make_client()
        calls: list[str] = []

        def fake_request(method, url, **kwargs):
            calls.append(method)
            if method == "GET":
                return FakeResponse(500, text="boom")
            return FakeResponse(204)

        with mock.patch.object(clashmi.requests, "request", side_effect=fake_request):
            closed = client.close_connections()

        self.assertEqual(calls, ["GET", "DELETE"])
        self.assertEqual(closed, -1)  # count unknown, DELETE still sent

    def test_returns_count_before_close(self) -> None:
        client = make_client()

        def fake_request(method, url, **kwargs):
            if method == "GET":
                return FakeResponse(200, {"connections": [{"id": 1}, {"id": 2}]})
            return FakeResponse(204)

        with mock.patch.object(clashmi.requests, "request", side_effect=fake_request):
            self.assertEqual(client.close_connections(), 2)

    def test_delete_timeout_raises_clashmi_error(self) -> None:
        client = make_client()

        def fake_request(method, url, **kwargs):
            if method == "GET":
                return FakeResponse(200, {"connections": []})
            raise requests.Timeout("timed out")

        with mock.patch.object(clashmi.requests, "request", side_effect=fake_request):
            with self.assertRaises(ClashMiError):
                client.close_connections()


class UnauthorizedRetryTests(unittest.TestCase):
    def tearDown(self) -> None:
        clashmi.invalidate_settings_cache()

    def test_401_rediscovers_and_retries_once(self) -> None:
        client = ClashMiClient(
            ClashMiSettings(base_url="http://127.0.0.1:9090", secret="old", source="test")
        )
        fresh = ClashMiSettings(
            base_url="http://127.0.0.1:9090", secret="new", source="rediscovered"
        )
        seen_auth: list[str | None] = []

        def fake_request(method, url, headers=None, **kwargs):
            seen_auth.append((headers or {}).get("Authorization"))
            if len(seen_auth) == 1:
                return FakeResponse(401)
            return FakeResponse(200, {"connections": [{"id": 1}]})

        with mock.patch.object(
            ClashMiClient,
            "discover_settings",
            staticmethod(lambda use_cache=True: fresh),
        ), mock.patch.object(clashmi.requests, "request", side_effect=fake_request):
            count = client.connection_count()

        self.assertEqual(count, 1)
        self.assertEqual(seen_auth, ["Bearer old", "Bearer new"])
        self.assertEqual(client.settings.secret, "new")

    def test_persistent_401_raises(self) -> None:
        client = make_client()

        def fake_request(method, url, **kwargs):
            return FakeResponse(401)

        with mock.patch.object(
            ClashMiClient,
            "discover_settings",
            staticmethod(lambda use_cache=True: client.settings),
        ), mock.patch.object(clashmi.requests, "request", side_effect=fake_request):
            with self.assertRaises(ClashMiError):
                client.connection_count()


if __name__ == "__main__":
    unittest.main()
