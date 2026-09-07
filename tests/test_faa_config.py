"""FAA NMS-API configuration.

Hosts and paths are asserted against the FAA's own onboarding pack — *NMS-API
cURL Command Examples and Instructions for connecting*, issued with API
registration. The overlay tests exist because that document will change: the
question they answer is whether an operator can follow it without waiting for
a release.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aeropub.faa.config import (
    CONFIG_PATH_VAR,
    ENVIRONMENTS,
    ENVIRONMENT_VAR,
    ClientCredentials,
    NmsEndpoint,
    NmsEnvironment,
    load_environment,
)
from aeropub.registry import CredentialStatus


class TestPublishedEnvironments:
    @pytest.mark.parametrize(
        "name,host",
        [
            ("sit", "https://api-sit.cgifederal-aim.com"),
            ("staging", "https://api-staging.cgifederal-aim.com"),
            ("prod", "https://api-nms.aim.faa.gov"),
        ],
    )
    def test_hosts_match_the_onboarding_pack(self, name, host):
        assert ENVIRONMENTS[name].host == host

    def test_the_api_base_is_nmsapi_not_the_bare_host(self):
        assert ENVIRONMENTS["staging"].base == (
            "https://api-staging.cgifederal-aim.com/nmsapi"
        )

    def test_the_token_endpoint_is_not_under_the_api_base(self):
        # The trap in the FAA's own examples: /v1/auth/token sits on the bare
        # host while every operation sits under /nmsapi. Building the token URL
        # from the API base gives a 404 that looks like a bad credential.
        env = ENVIRONMENTS["staging"]
        assert env.token_url == "https://api-staging.cgifederal-aim.com/v1/auth/token"
        assert "/nmsapi" not in env.token_url

    @pytest.mark.parametrize(
        "endpoint,expected",
        [
            ("ping", "https://api-nms.aim.faa.gov/nmsapi/v1/ping"),
            ("location_series", "https://api-nms.aim.faa.gov/nmsapi/v1/locationseries"),
            ("notams", "https://api-nms.aim.faa.gov/nmsapi/v1/notams"),
            ("notam_checklist", "https://api-nms.aim.faa.gov/nmsapi/v1/notams/checklist"),
            ("initial_load", "https://api-nms.aim.faa.gov/nmsapi/v1/notams/il"),
        ],
    )
    def test_operation_urls_match_the_documented_paths(self, endpoint, expected):
        assert ENVIRONMENTS["prod"].url(endpoint) == expected

    def test_the_notam_query_carries_the_required_header(self):
        # The API rejects /notams without it, with an error that does not
        # mention the header. Carrying it on the endpoint means no call site
        # can forget it.
        assert ENVIRONMENTS["prod"].endpoint("notams").headers == {
            "nmsResponseFormat": "AIXM"
        }

    def test_only_production_is_marked_production(self):
        assert ENVIRONMENTS["prod"].is_production
        assert not ENVIRONMENTS["sit"].is_production
        assert not ENVIRONMENTS["staging"].is_production


class TestInitialLoadAddressing:
    def test_whole_feed_and_single_classification(self):
        env = ENVIRONMENTS["staging"]
        assert env.initial_load_url().endswith("/nmsapi/v1/notams/il")
        assert env.initial_load_url("DOMESTIC").endswith("/nmsapi/v1/notams/il/DOMESTIC")

    def test_a_classification_is_normalised_but_never_rejected(self):
        # If the FAA adds a classification, refusing to ask for it would look
        # exactly like the FAA not publishing it.
        env = ENVIRONMENTS["staging"]
        assert env.initial_load_url(" domestic ").endswith("/il/DOMESTIC")
        assert env.initial_load_url("SOMETHING_NEW").endswith("/il/SOMETHING_NEW")


class TestValidation:
    def test_a_plaintext_host_is_refused(self):
        # A bearer token over http is a disclosed bearer token, and no test
        # environment is worth relaxing that for.
        with pytest.raises(ValueError, match="https"):
            NmsEnvironment(name="local", host="http://api.example.gov")

    def test_a_host_carrying_a_path_is_refused(self):
        with pytest.raises(ValueError, match="scheme and host"):
            NmsEnvironment(name="x", host="https://api.example.gov/nmsapi")

    def test_paths_must_be_absolute(self):
        with pytest.raises(ValueError, match="must start with"):
            NmsEnvironment(name="x", host="https://a.example.gov", api_base="nmsapi")

    def test_duplicate_endpoint_names_are_refused(self):
        with pytest.raises(ValueError, match="duplicate endpoint"):
            NmsEnvironment(
                name="x",
                host="https://a.example.gov",
                endpoints=(
                    NmsEndpoint(name="ping", path="/v1/ping"),
                    NmsEndpoint(name="ping", path="/v2/ping"),
                ),
            )

    def test_an_unknown_endpoint_names_what_is_configured(self):
        with pytest.raises(KeyError, match="notams"):
            ENVIRONMENTS["prod"].endpoint("nowhere")

    def test_a_missing_path_parameter_says_which_one(self):
        with pytest.raises(ValueError, match="classification"):
            ENVIRONMENTS["prod"].endpoint("initial_load_by_classification").format()


class TestOverlay:
    def test_a_moved_host_needs_no_code_change(self):
        moved = ENVIRONMENTS["prod"].overlay({"host": "https://nms2.aim.faa.gov"})
        assert moved.url("ping") == "https://nms2.aim.faa.gov/nmsapi/v1/ping"
        assert moved.token_url == "https://nms2.aim.faa.gov/v1/auth/token"

    def test_a_renamed_path_corrects_one_endpoint_and_leaves_the_rest(self):
        # Replacing the endpoint list wholesale would mean an operator fixing
        # one path silently dropped the other five.
        patched = ENVIRONMENTS["prod"].overlay(
            {"endpoints": [{"name": "ping", "path": "/v2/health"}]}
        )
        assert patched.url("ping") == "https://api-nms.aim.faa.gov/nmsapi/v2/health"
        assert patched.url("notams") == "https://api-nms.aim.faa.gov/nmsapi/v1/notams"
        assert len(patched.endpoints) == len(ENVIRONMENTS["prod"].endpoints)

    def test_an_overlaid_endpoint_keeps_headers_it_does_not_mention(self):
        patched = ENVIRONMENTS["prod"].overlay(
            {"endpoints": [{"name": "notams", "path": "/v2/notams"}]}
        )
        assert patched.endpoint("notams").headers == {"nmsResponseFormat": "AIXM"}

    def test_a_new_required_header_can_be_added(self):
        patched = ENVIRONMENTS["prod"].overlay(
            {
                "endpoints": [
                    {
                        "name": "notams",
                        "path": "/v1/notams",
                        "headers": {"nmsResponseFormat": "AIXM", "X-FAA-Version": "2"},
                    }
                ]
            }
        )
        assert patched.endpoint("notams").headers["X-FAA-Version"] == "2"

    def test_a_wholly_new_endpoint_can_be_added(self):
        patched = ENVIRONMENTS["prod"].overlay(
            {"endpoints": [{"name": "digital_notam", "path": "/v1/dnotam"}]}
        )
        assert patched.url("digital_notam").endswith("/nmsapi/v1/dnotam")

    def test_the_overlay_round_trips_through_json(self):
        original = ENVIRONMENTS["staging"]
        restored = NmsEnvironment(
            name="x", host="https://a.example.gov"
        ).overlay(json.loads(json.dumps(original.to_dict())))
        assert restored.to_dict() == original.to_dict()


class TestLoadEnvironment:
    def test_defaults_to_production(self):
        assert load_environment(environ={}).name == "prod"

    def test_the_environment_variable_selects(self):
        assert load_environment(environ={ENVIRONMENT_VAR: "sit"}).name == "sit"

    def test_an_explicit_name_beats_the_environment_variable(self):
        assert load_environment("staging", environ={ENVIRONMENT_VAR: "sit"}).name == "staging"

    def test_an_unknown_name_says_how_to_add_one(self):
        with pytest.raises(KeyError, match=CONFIG_PATH_VAR):
            load_environment("mars", environ={})

    def test_a_config_file_overlays_the_built_in(self, tmp_path):
        config = tmp_path / "nms.json"
        config.write_text(json.dumps({"base": "staging", "host": "https://moved.example.gov"}))
        env = load_environment(environ={CONFIG_PATH_VAR: str(config)})
        assert env.name == "staging"
        assert env.host == "https://moved.example.gov"
        assert env.url("ping") == "https://moved.example.gov/nmsapi/v1/ping"

    def test_a_config_file_that_is_not_an_object_is_refused(self, tmp_path):
        config = tmp_path / "nms.json"
        config.write_text("[]")
        with pytest.raises(ValueError, match="JSON object"):
            load_environment(environ={CONFIG_PATH_VAR: str(config)})


def isolated(environ=None, tmp_path=None):
    """A store that reads the given mapping and no real credentials file.

    Without the path override every one of these would read whatever the
    developer happens to have installed in ``~/.aeropub``, and would pass or
    fail by machine.
    """
    from aeropub.credentials import CredentialStore

    return CredentialStore(
        environ=dict(environ or {}),
        path=(tmp_path / "nothing.json") if tmp_path is not None else Path("/nonexistent/aeropub.json"),
    )


class TestClientCredentials:
    def test_the_current_names_are_the_documented_ones(self):
        """The name in the docs, the name `aeropub credentials` offers, and
        the name the connector reads have to be one name."""
        creds = ClientCredentials.default()
        assert creds.client_id.env_var == "AEROPUB_FAA_CLIENT_ID"
        assert creds.client_secret.env_var == "AEROPUB_FAA_CLIENT_SECRET"
        assert "KEY" in creds.client_id.label
        assert "SECRET" in creds.client_secret.label

    def test_the_earlier_names_are_still_read(self):
        """A rename that only changed the code would break every working
        installation on the next upgrade."""
        creds = ClientCredentials.default()
        assert creds.client_id.aliases == ("FAA_NMS_CLIENT_ID",)
        held = isolated({"FAA_NMS_CLIENT_ID": "k", "FAA_NMS_CLIENT_SECRET": "s"})
        assert creds.resolve(store=held) == ("k", "s")

    def test_an_old_name_is_reported_rather_than_silently_accepted(self):
        """Two live names for one secret is how a rotated credential loses to
        a stale one."""
        creds = ClientCredentials.default()
        held = isolated({"FAA_NMS_CLIENT_ID": "k", "AEROPUB_FAA_CLIENT_SECRET": "s"})
        assert creds.deprecated_names(store=held) == (
            ("FAA_NMS_CLIENT_ID", "AEROPUB_FAA_CLIENT_ID"),
        )

    def test_the_current_name_wins_over_the_old_one(self):
        creds = ClientCredentials.default()
        held = isolated(
            {"AEROPUB_FAA_CLIENT_ID": "new", "FAA_NMS_CLIENT_ID": "old",
             "AEROPUB_FAA_CLIENT_SECRET": "s"}
        )
        assert creds.resolve(store=held) == ("new", "s")
        assert creds.deprecated_names(store=held) == ()

    def test_resolve_needs_both_halves(self):
        creds = ClientCredentials.default()
        assert creds.resolve(store=isolated({"AEROPUB_FAA_CLIENT_ID": "k"})) is None
        assert creds.resolve(store=isolated({"AEROPUB_FAA_CLIENT_SECRET": "s"})) is None
        both = isolated(
            {"AEROPUB_FAA_CLIENT_ID": "k", "AEROPUB_FAA_CLIENT_SECRET": "s"}
        )
        assert creds.resolve(store=both) == ("k", "s")

    def test_missing_names_the_absent_half(self):
        # Half a pair installed is the commonest onboarding mistake, and it
        # produces a 401 that says nothing about which half.
        creds = ClientCredentials.default()
        assert creds.missing(store=isolated({"AEROPUB_FAA_CLIENT_ID": "k"})) == (
            "AEROPUB_FAA_CLIENT_SECRET",
        )

    def test_missing_names_the_current_name_even_when_the_old_one_is_set(self):
        """The operator is being told what to set next, not what they set
        last."""
        creds = ClientCredentials.default()
        assert creds.missing(store=isolated({"FAA_NMS_CLIENT_ID": "k"})) == (
            "AEROPUB_FAA_CLIENT_SECRET",
        )

    def test_a_credential_set_through_the_documented_command_is_found(self, tmp_path):
        """The defect this replaced: `aeropub credentials --set` writes to a
        file outside any repository, and the connector read only the
        environment — so the documented way to install a credential produced a
        connector that reported it missing.
        """
        from aeropub.credentials import CredentialStore

        store = CredentialStore(environ={}, path=tmp_path / "credentials.json")
        store.set_secret("AEROPUB_FAA_CLIENT_ID", "from-the-file")
        store.set_secret("AEROPUB_FAA_CLIENT_SECRET", "also-from-the-file")

        creds = ClientCredentials.default()
        assert creds.resolve(store=store) == ("from-the-file", "also-from-the-file")
        assert creds.missing(store=store) == ()

    def test_the_environment_still_wins_over_the_file(self, tmp_path):
        """Hosted deployments set real environment variables, and those are
        the ones that survive a restart."""
        from aeropub.credentials import CredentialStore

        store = CredentialStore(
            environ={"AEROPUB_FAA_CLIENT_ID": "from-the-environment"},
            path=tmp_path / "credentials.json",
        )
        store.set_secret("AEROPUB_FAA_CLIENT_ID", "from-the-file")
        store.set_secret("AEROPUB_FAA_CLIENT_SECRET", "s")
        creds = ClientCredentials.default()
        assert creds.resolve(store=store) == ("from-the-environment", "s")

    def test_status_is_the_worse_of_the_two(self):
        creds = ClientCredentials.default()
        assert creds.status(store=isolated()) is CredentialStatus.MISSING
        assert (
            creds.status(store=isolated({"AEROPUB_FAA_CLIENT_ID": "k"}))
            is CredentialStatus.MISSING
        )
        both = isolated(
            {"AEROPUB_FAA_CLIENT_ID": "k", "AEROPUB_FAA_CLIENT_SECRET": "s"}
        )
        assert creds.status(store=both) is CredentialStatus.UNVERIFIED
        assert creds.status(store=both, rejected=True) is CredentialStatus.INVALID

    def test_no_configuration_object_can_carry_a_secret(self):
        creds = ClientCredentials.default()
        rendered = repr(creds) + repr(ENVIRONMENTS["prod"].to_dict())
        assert "FAA_NMS_CLIENT_SECRET" in rendered  # the name is fine
        for ref in creds.refs():
            assert ref.hint is None
