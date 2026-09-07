"""The console `docs/faa-nms.md` sends an operator to first.

Separate from `test_netcheck.py` because that module needs openssl to mint a
certificate and skips itself without one. Nothing here opens a socket, and a
CI without openssl still has to be able to prove that a blocked host does not
report as a pass and does not send anybody to rotate a working credential.
"""

from __future__ import annotations

from aeropub.netcheck import (
    EXIT_NETWORK_POLICY,
    EXIT_OK,
    EXIT_OURS,
    EXIT_UNREACHABLE,
    Layer,
    Probe,
    main,
    report,
    verdict,
)


def reachable(host: str = "api-nms.aim.faa.gov") -> Probe:
    return Probe(
        url=f"https://{host}/nmsapi/v1/ping",
        host=host,
        layer=Layer.OK,
        http_status=401,
        detail="reachable (HTTP 401 without a credential, as expected)",
        proxy="http://proxy:3128",
        duration_ms=42,
    )


def blocked(host: str = "api-nms.aim.faa.gov", layer: Layer = Layer.PROXY_DENIED) -> Probe:
    return Probe(
        url=f"https://{host}/nmsapi/v1/ping",
        host=host,
        layer=layer,
        detail="egress proxy answered 403 to CONNECT",
        proxy="http://proxy:3128",
        duration_ms=7,
    )


class TestReport:
    """The command `docs/faa-nms.md` sends an operator to first."""

    def test_a_reachable_host_says_the_network_is_not_the_problem(self):
        page = report([reachable()])
        assert "ok" in page
        assert "not the network" in page

    def test_a_blocked_host_names_who_can_fix_it(self):
        page = report([blocked()])
        assert "your network administrator" in page
        assert "api-nms.aim.faa.gov:443" in page

    def test_our_own_failure_is_not_sent_to_the_network_team(self):
        page = report([blocked(layer=Layer.TLS_UNTRUSTED)])
        assert "your network administrator" not in page
        assert "this side" in page

    def test_it_says_no_credential_was_tested(self):
        """The commonest wrong move after a blocked host is to go and rotate a
        perfectly good key."""
        page = report([blocked()])
        assert "nothing about whether a key is valid" in page

    def test_a_reachable_run_does_not_carry_the_credential_warning(self):
        assert "nothing about whether a key is valid" not in report([reachable()])

    def test_the_proxy_and_bundle_are_shown_once(self):
        page = report([blocked("a.example"), blocked("b.example")])
        assert page.count("http://proxy:3128\n") == 1

    def test_every_host_gets_a_line(self):
        page = report([reachable("a.example"), blocked("b.example")])
        assert "a.example" in page and "b.example" in page

    def test_nothing_to_probe_is_said_rather_than_shown_as_success(self):
        assert report([]) == "nothing to probe"


class TestVerdict:
    def test_all_reachable_is_zero(self):
        assert verdict([reachable(), reachable("b.example")]) == EXIT_OK

    def test_a_policy_denial_has_its_own_code(self):
        """A blocked host and a broken one do not go to the same team, so a
        health check has to be able to tell them apart without reading prose."""
        assert verdict([blocked()]) == EXIT_NETWORK_POLICY

    def test_our_own_configuration_has_its_own_code(self):
        assert verdict([blocked(layer=Layer.TLS_UNTRUSTED)]) == EXIT_OURS

    def test_the_authoritys_own_refusal_is_neither(self):
        assert verdict([blocked(layer=Layer.REFUSED)]) == EXIT_UNREACHABLE

    def test_one_blocked_host_fails_the_run(self):
        assert verdict([reachable(), blocked()]) == EXIT_NETWORK_POLICY

    def test_nothing_probed_is_not_a_pass(self):
        assert verdict([]) == EXIT_UNREACHABLE


class TestMain:
    def test_a_bare_hostname_is_taken_as_https(self, monkeypatch):
        seen: list[str] = []

        def fake(url, **kwargs):
            seen.append(url)
            return reachable()

        monkeypatch.setattr("aeropub.netcheck.probe", fake)
        assert main(["example.test"]) == EXIT_OK
        assert seen == ["https://example.test/"]

    def test_a_full_url_is_left_alone(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            "aeropub.netcheck.probe",
            lambda url, **kw: (seen.append(url), reachable())[1],
        )
        main(["https://example.test/v1/ping"])
        assert seen == ["https://example.test/v1/ping"]

    def test_with_no_argument_it_probes_the_faa_environment(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            "aeropub.netcheck.probe",
            lambda url, **kw: (seen.append(url), reachable())[1],
        )
        main([])
        assert seen and "/v1/ping" in seen[0]

    def test_all_probes_every_environment(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(
            "aeropub.netcheck.probe",
            lambda url, **kw: (seen.append(url), reachable())[1],
        )
        main(["--all"])
        assert len(seen) >= 3

    def test_it_exits_non_zero_when_a_host_is_blocked(self, monkeypatch):
        monkeypatch.setattr("aeropub.netcheck.probe", lambda url, **kw: blocked())
        assert main(["example.test"]) == EXIT_NETWORK_POLICY

    def test_no_credential_is_read_or_sent(self, monkeypatch):
        """A reachability probe that needed a key could not answer the
        question it exists for — whether the key is the problem."""
        monkeypatch.setenv("AEROPUB_FAA_CLIENT_ID", "must-not-be-read")
        monkeypatch.setenv("AEROPUB_FAA_CLIENT_SECRET", "must-not-be-read")
        headers: list[dict] = []

        def fake(url, *, timeout=20, environ=None, opener=None, user_agent=""):
            headers.append({"user_agent": user_agent})
            return reachable()

        monkeypatch.setattr("aeropub.netcheck.probe", fake)
        main(["example.test"])
        assert all("must-not-be-read" not in str(h) for h in headers)
