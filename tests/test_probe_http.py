from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

from claude_rotate.accounts import Account
from claude_rotate.probe import ProbeResult, fetch_usage, probe_many

FIX = Path(__file__).parent / "fixtures"

_NOW = 1_776_854_321


def _make_response(status: int, body: bytes = b"{}") -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.status = status
    resp.read = MagicMock(return_value=body)
    return resp


def _usage_body() -> bytes:
    return (FIX / "usage_max20.json").read_bytes()


def test_fetch_usage_200_returns_parsed_result() -> None:
    with patch(
        "claude_rotate.probe.urllib.request.urlopen",
        return_value=_make_response(200, _usage_body()),
    ):
        r = fetch_usage("sk-ant-oat01-zzz", now=_NOW)
    assert r.ok
    assert r.h5_pct == 8.0
    assert r.w7_pct == 89.0


def test_fetch_usage_401_returns_unauthorized() -> None:
    import urllib.error

    err = urllib.error.HTTPError(url="http://x", code=401, msg="u", hdrs={}, fp=io.BytesIO(b"{}"))
    with patch("claude_rotate.probe.urllib.request.urlopen", side_effect=err):
        r = fetch_usage("sk-ant-oat01-zzz")
    assert not r.ok
    assert r.error == "unauthorized"


def test_fetch_usage_403_returns_unauthorized() -> None:
    import urllib.error

    err = urllib.error.HTTPError(url="http://x", code=403, msg="f", hdrs={}, fp=io.BytesIO(b"{}"))
    with patch("claude_rotate.probe.urllib.request.urlopen", side_effect=err):
        r = fetch_usage("sk-ant-oat01-zzz")
    assert not r.ok
    assert r.error == "unauthorized"


def test_fetch_usage_429_returns_rate_limited() -> None:
    import urllib.error

    err = urllib.error.HTTPError(url="http://x", code=429, msg="rl", hdrs={}, fp=io.BytesIO(b"{}"))
    with patch("claude_rotate.probe.urllib.request.urlopen", side_effect=err):
        r = fetch_usage("sk-ant-oat01-zzz")
    assert not r.ok
    assert r.error == "rate_limited"


def test_fetch_usage_500_returns_upstream_error() -> None:
    import urllib.error

    err = urllib.error.HTTPError(url="http://x", code=503, msg="sv", hdrs={}, fp=io.BytesIO(b"{}"))
    with patch("claude_rotate.probe.urllib.request.urlopen", side_effect=err):
        r = fetch_usage("sk-ant-oat01-zzz")
    assert not r.ok
    assert r.error == "upstream_error"


def test_fetch_usage_timeout_returns_network_error() -> None:
    with patch(
        "claude_rotate.probe.urllib.request.urlopen",
        side_effect=TimeoutError("timed out"),
    ):
        r = fetch_usage("sk-ant-oat01-zzz")
    assert not r.ok
    assert r.error.startswith("network_error")


def test_fetch_usage_sends_bearer_token() -> None:
    captured: list = []

    def _mock_open(req, timeout=None):  # type: ignore[no-untyped-def]
        captured.append(req)
        return _make_response(200, _usage_body())

    with patch("claude_rotate.probe.urllib.request.urlopen", side_effect=_mock_open):
        fetch_usage("sk-ant-oat01-mytoken", now=_NOW)

    req = captured[0]
    assert req.headers.get("Authorization") == "Bearer sk-ant-oat01-mytoken"


def test_fetch_usage_sends_anthropic_beta() -> None:
    captured: list = []

    def _mock_open(req, timeout=None):  # type: ignore[no-untyped-def]
        captured.append(req)
        return _make_response(200, _usage_body())

    with patch("claude_rotate.probe.urllib.request.urlopen", side_effect=_mock_open):
        fetch_usage("sk-ant-oat01-tok", now=_NOW)

    req = captured[0]
    assert "oauth" in req.headers.get("Anthropic-beta", "")


def test_probe_many_calls_fetch_usage_per_account() -> None:
    from datetime import UTC, datetime

    def _acc(name: str) -> Account:
        return Account(
            name=name,
            runtime_token=f"sk-ant-oat01-{name}",
            label=name,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            plan="max_20x",
        )

    results = {"a": 0, "b": 0}

    def fake_fetch(token: str, **kw: object) -> ProbeResult:
        name = token.split("-")[-1]
        results[name] += 1
        return ProbeResult(
            ok=True, http_code=200, h5_pct=10.0, w7_pct=10.0, h5_reset_secs=100, w7_reset_secs=200
        )

    with patch("claude_rotate.probe.fetch_usage", side_effect=fake_fetch):
        out = probe_many([_acc("a"), _acc("b")])

    assert {r.account.name for r in out} == {"a", "b"}
    assert results == {"a": 1, "b": 1}


def test_probe_many_passes_probe_error_on_failure() -> None:
    """probe_many sets probe_error on candidates whose fetch returns ok=False."""
    from datetime import UTC, datetime

    def _acc(name: str) -> Account:
        return Account(
            name=name,
            runtime_token=f"sk-ant-oat01-{name}",
            label=name,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            plan="max_20x",
        )

    def fake_fetch(token: str, **kw: object) -> ProbeResult:
        name = token.split("-")[-1]
        if name == "ok":
            return ProbeResult(
                ok=True,
                http_code=200,
                h5_pct=10.0,
                w7_pct=10.0,
                h5_reset_secs=100,
                w7_reset_secs=200,
            )
        return ProbeResult(ok=False, http_code=429, error="rate_limited")

    with patch("claude_rotate.probe.fetch_usage", side_effect=fake_fetch):
        out = probe_many([_acc("ok"), _acc("rl")])

    by_name = {c.account.name: c for c in out}
    assert by_name["ok"].probe_error == ""
    assert by_name["rl"].probe_error == "rate_limited"
    assert by_name["rl"].h5_pct is None
    assert by_name["rl"].w7_pct is None


def test_probe_many_empty_probe_error_on_success() -> None:
    """Successful fetch produces probe_error='' (no error)."""
    from datetime import UTC, datetime

    acc = Account(
        name="good",
        runtime_token="sk-ant-oat01-good",
        label="good",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        plan="max_20x",
    )

    def fake_fetch(_token: str, **kw: object) -> ProbeResult:
        return ProbeResult(
            ok=True,
            http_code=200,
            h5_pct=5.0,
            w7_pct=15.0,
            h5_reset_secs=3600,
            w7_reset_secs=86400,
        )

    with patch("claude_rotate.probe.fetch_usage", side_effect=fake_fetch):
        out = probe_many([acc])

    assert out[0].probe_error == ""
