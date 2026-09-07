"""Reading a credential out of the pack an authority ships.

The hazard this module exists for is not parsing. It is the copy: an operator
opens the FAA's SoapUI project, finds a 64-character opaque string, and moves
it by hand into a terminal, a chat window or a screenshot. So most of what is
asserted here is about what never appears — in a summary, in a repr, in an
exception — rather than about what is read.

The pack below is synthetic. The real one carries live credentials and is not
in this repository, which `tests/test_credentials.py` enforces on every run.
"""

from __future__ import annotations

import pytest

from aeropub.onboarding import OnboardingPack, PackError, read_soapui_pack

# Obvious fakes, the real lengths, and built from parts on purpose: a literal
# of this shape in a tracked file is exactly what test_credentials.py scans
# for, and a fixture that has to be exempted from the credential scanner is a
# hole in it. Assembled here, nothing in this file matches.
FAKE_ID = "NOT-A-REAL-ID-" + ("x" * 34)
FAKE_SECRET = "NOT-A-REAL-SECRET-" + ("y" * 46)
FAKE_TOKEN = "NOT-A-REAL-TOKEN-" + ("z" * 11)

assert (len(FAKE_ID), len(FAKE_SECRET)) == (48, 64)  # the FAA's own lengths


def pack_xml(
    *,
    client_id: str | None = FAKE_ID,
    client_secret: str | None = FAKE_SECRET,
    token: str | None = FAKE_TOKEN,
    host: str = "https://api-staging.cgifederal-aim.com",
) -> str:
    def element(tag, value):
        return f"<con:{tag}>{value}</con:{tag}>" if value is not None else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<con:soapui-project xmlns:con="http://eviware.com/soapui/config" name="NMS-API">
  <con:endpoint>{host}/nmsapi</con:endpoint>
  <con:oAuth2ProfileContainer>
    <con:oAuth2Profile>
      <con:name>PreProd</con:name>
      {element("clientID", client_id)}
      {element("clientSecret", client_secret)}
      {element("accessToken", token)}
      <con:accessTokenURI>{host}/v1/auth/token</con:accessTokenURI>
    </con:oAuth2Profile>
  </con:oAuth2ProfileContainer>
</con:soapui-project>
"""


def write(tmp_path, xml: str, name: str = "project.xml"):
    path = tmp_path / name
    path.write_text(xml, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# What it reads
# --------------------------------------------------------------------------


class TestReading:
    def test_both_halves_are_read_under_the_current_names(self, tmp_path):
        pack = read_soapui_pack(write(tmp_path, pack_xml()))
        assert pack.secrets == {
            "AEROPUB_FAA_CLIENT_ID": FAKE_ID,
            "AEROPUB_FAA_CLIENT_SECRET": FAKE_SECRET,
        }

    def test_the_token_url_and_endpoint_come_along(self, tmp_path):
        pack = read_soapui_pack(write(tmp_path, pack_xml()))
        assert pack.token_url.endswith("/v1/auth/token")
        assert pack.endpoint.endswith("/nmsapi")

    def test_the_environment_is_read_off_the_packs_own_host(self, tmp_path):
        """A pack is issued for one environment, and its keys against another
        give a 401 that says nothing about why."""
        assert read_soapui_pack(write(tmp_path, pack_xml())).environment_hint == "staging"

    def test_a_sit_pack_is_recognised(self, tmp_path):
        xml = pack_xml(host="https://api-sit.cgifederal-aim.com")
        assert read_soapui_pack(write(tmp_path, xml)).environment_hint == "fit"

    def test_a_production_pack_is_recognised(self, tmp_path):
        xml = pack_xml(host="https://api-nms.aim.faa.gov")
        assert read_soapui_pack(write(tmp_path, xml)).environment_hint == "prod"

    def test_an_unrecognised_host_claims_no_environment(self, tmp_path):
        xml = pack_xml(host="https://gateway.example")
        assert read_soapui_pack(write(tmp_path, xml)).environment_hint == ""


class TestTheAccessToken:
    def test_it_is_never_imported(self, tmp_path):
        """A bearer in an exported project is minutes old at best, and the
        client mints its own from the id and secret."""
        pack = read_soapui_pack(write(tmp_path, pack_xml()))
        assert FAKE_TOKEN not in pack.secrets.values()
        assert "accessToken" in pack.dropped

    def test_its_presence_is_still_reported(self, tmp_path):
        """The operator needs to know the pack is hazardous."""
        pack = read_soapui_pack(write(tmp_path, pack_xml()))
        assert "accessToken" in pack.summary()

    def test_a_pack_without_one_drops_nothing(self, tmp_path):
        pack = read_soapui_pack(write(tmp_path, pack_xml(token=None)))
        assert pack.dropped == ()


# --------------------------------------------------------------------------
# What never appears
# --------------------------------------------------------------------------


class TestDisclosure:
    def test_the_summary_gives_a_length_and_not_a_value(self, tmp_path):
        summary = read_soapui_pack(write(tmp_path, pack_xml())).summary()
        assert FAKE_SECRET not in summary
        assert FAKE_ID not in summary
        assert f"({len(FAKE_SECRET)} characters)" in summary

    def test_the_repr_carries_names_and_not_values(self, tmp_path):
        """A dataclass that printed itself into a log would undo the point."""
        rendered = repr(read_soapui_pack(write(tmp_path, pack_xml())))
        assert FAKE_SECRET not in rendered
        assert FAKE_ID not in rendered
        assert "AEROPUB_FAA_CLIENT_ID" in rendered

    def test_the_repr_of_the_field_itself_is_suppressed(self):
        pack = OnboardingPack(path="x", secrets={"A": "shhh"})
        assert "shhh" not in repr(pack)

    def test_an_error_never_quotes_the_file(self, tmp_path):
        """A parse failure that echoed the offending line would print the
        credential in the one situation where somebody copies the output into
        a bug report."""
        path = write(tmp_path, pack_xml(client_secret=None))
        with pytest.raises(PackError) as caught:
            read_soapui_pack(path)
        assert FAKE_ID not in str(caught.value)


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


class TestRefusals:
    def test_half_a_pair_is_refused_rather_than_half_imported(self, tmp_path):
        """A half-installed pair produces a 401 that says nothing about which
        half is wrong."""
        path = write(tmp_path, pack_xml(client_secret=None))
        with pytest.raises(PackError, match="AEROPUB_FAA_CLIENT_SECRET"):
            read_soapui_pack(path)

    def test_a_pack_exported_before_the_keys_were_issued_says_so(self, tmp_path):
        path = write(tmp_path, pack_xml(client_id=None, client_secret=None))
        with pytest.raises(PackError, match="before the keys were issued"):
            read_soapui_pack(path)

    def test_something_that_is_not_a_soapui_project_is_refused(self, tmp_path):
        path = write(tmp_path, "<xml>not a project</xml>")
        with pytest.raises(PackError, match="not a SoapUI project"):
            read_soapui_pack(path)

    def test_it_points_at_the_ordinary_command_instead(self, tmp_path):
        path = write(tmp_path, "<xml>not a project</xml>")
        with pytest.raises(PackError, match="credentials --set"):
            read_soapui_pack(path)

    def test_a_missing_file_is_an_error_not_an_empty_pack(self, tmp_path):
        with pytest.raises(PackError, match="cannot be read"):
            read_soapui_pack(tmp_path / "nothing.xml")

    def test_an_empty_value_counts_as_absent(self, tmp_path):
        path = write(tmp_path, pack_xml(client_secret="   "))
        with pytest.raises(PackError, match="AEROPUB_FAA_CLIENT_SECRET"):
            read_soapui_pack(path)


# --------------------------------------------------------------------------
# Through the command
# --------------------------------------------------------------------------


class TestTheCommand:
    def run(self, argv, tmp_path, monkeypatch, capsys):
        from aeropub.cli import main
        from aeropub.credentials import CREDENTIALS_PATH_VAR

        monkeypatch.setenv(CREDENTIALS_PATH_VAR, str(tmp_path / "credentials.json"))
        code = main(argv)
        return code, capsys.readouterr().out

    def test_a_dry_run_writes_nothing(self, tmp_path, monkeypatch, capsys):
        path = write(tmp_path, pack_xml())
        code, out = self.run(
            ["credentials", "--import-pack", str(path), "--dry-run"],
            tmp_path, monkeypatch, capsys,
        )
        assert code == 0
        assert "nothing was written" in out
        assert not (tmp_path / "credentials.json").exists()

    def test_an_import_installs_both_halves(self, tmp_path, monkeypatch, capsys):
        from aeropub.credentials import CredentialStore

        path = write(tmp_path, pack_xml())
        code, out = self.run(
            ["credentials", "--import-pack", str(path)], tmp_path, monkeypatch, capsys
        )
        assert code == 0
        store = CredentialStore(environ={}, path=tmp_path / "credentials.json")
        assert store.get("AEROPUB_FAA_CLIENT_ID") == FAKE_ID
        assert store.get("AEROPUB_FAA_CLIENT_SECRET") == FAKE_SECRET

    def test_the_console_never_renders_a_secret(self, tmp_path, monkeypatch, capsys):
        """The whole reason this command exists rather than a copy and paste."""
        path = write(tmp_path, pack_xml())
        _, out = self.run(
            ["credentials", "--import-pack", str(path)], tmp_path, monkeypatch, capsys
        )
        assert FAKE_SECRET not in out
        assert FAKE_ID not in out
        assert FAKE_TOKEN not in out

    def test_it_names_the_environment_the_pack_is_for(self, tmp_path, monkeypatch, capsys):
        path = write(tmp_path, pack_xml())
        _, out = self.run(
            ["credentials", "--import-pack", str(path)], tmp_path, monkeypatch, capsys
        )
        assert "FAA_NMS_ENVIRONMENT=staging" in out

    def test_it_says_to_rotate_every_time(self, tmp_path, monkeypatch, capsys):
        """A file that has been emailed carries a credential that has been
        emailed, and only the authority can fix that."""
        path = write(tmp_path, pack_xml())
        _, out = self.run(
            ["credentials", "--import-pack", str(path)], tmp_path, monkeypatch, capsys
        )
        assert "ROTATE" in out
        assert "7-AWA-NAIMES@faa.gov" in out

    def test_a_bad_pack_exits_non_zero_and_writes_nothing(
        self, tmp_path, monkeypatch, capsys
    ):
        path = write(tmp_path, "<xml>not a project</xml>")
        code, _ = self.run(
            ["credentials", "--import-pack", str(path)], tmp_path, monkeypatch, capsys
        )
        assert code != 0
        assert not (tmp_path / "credentials.json").exists()
