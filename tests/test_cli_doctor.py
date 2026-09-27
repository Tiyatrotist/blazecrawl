from __future__ import annotations

import argparse

from blazecrawl_core import cli


def test_doctor_returns_zero_when_all_checks_pass(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_doctor_playwright", lambda: (True, "chromium ok"))
    monkeypatch.setattr(cli, "_doctor_server", lambda: (True, "server ok"))
    monkeypatch.setattr(cli, "_doctor_api_key", lambda: (True, "key ok"))

    assert cli.cmd_doctor(argparse.Namespace()) == 0

    output = capsys.readouterr().out
    assert "[PASS] Playwright/Chromium: chromium ok" in output
    assert "[PASS] Server readiness: server ok" in output
    assert "[PASS] API key: key ok" in output


def test_doctor_returns_one_when_any_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_doctor_playwright", lambda: (True, "chromium ok"))
    monkeypatch.setattr(cli, "_doctor_server", lambda: (False, "server down"))
    monkeypatch.setattr(cli, "_doctor_api_key", lambda: (True, "key skipped"))

    assert cli.cmd_doctor(argparse.Namespace()) == 1

    output = capsys.readouterr().out
    assert "[FAIL] Server readiness: server down" in output


def test_doctor_api_key_treats_authenticated_not_found_as_valid(monkeypatch):
    class Response:
        status_code = 404

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, path):
            assert path == "/v1/crawl/__doctor__"
            return Response()

    monkeypatch.setenv("BLAZECRAWL_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("BLAZECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(cli, "_client", lambda: Client())

    assert cli._doctor_api_key() == (True, "API key accepted by the server")


def test_doctor_skips_remote_checks_when_not_configured(monkeypatch):
    monkeypatch.delenv("BLAZECRAWL_API_URL", raising=False)
    monkeypatch.delenv("BLAZECRAWL_API_KEY", raising=False)

    server_ok, server_detail = cli._doctor_server()
    key_ok, key_detail = cli._doctor_api_key()

    assert server_ok is True
    assert "skipped" in server_detail
    assert key_ok is True
    assert "skipped" in key_detail
