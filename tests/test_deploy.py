"""Deployment config is code, so it gets assertions like code.

None of this can prove systemd will accept the unit; `systemd-analyze verify`
does that on the host. What it does prove is that the hardening people think
they shipped is still in the file, and that the unit and the app agree on one
number that bites in production: the writable log path.
"""
import configparser
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNIT = os.path.join(ROOT, "deploy", "kb-advisor.service")
CADDY = os.path.join(ROOT, "deploy", "Caddyfile")
ENV_EXAMPLE = os.path.join(ROOT, "deploy", "kb.env.example")


def parse_unit():
    parser = configparser.ConfigParser(strict=False)
    parser.optionxform = str          # systemd keys are case-sensitive
    with open(UNIT) as fh:
        parser.read_file(fh)
    return parser


class TestUnitHardening:
    def test_runs_as_an_unprivileged_user(self):
        unit = parse_unit()["Service"]
        assert unit["User"] == "kb-advisor"
        assert unit.get("Group") == "kb-advisor"

    def test_restarts_even_on_a_clean_exit(self):
        unit = parse_unit()["Service"]
        assert unit["Restart"] == "always"

    def test_filesystem_is_read_only_except_the_log_dir(self):
        unit = parse_unit()["Service"]
        assert unit["ProtectSystem"] == "strict"
        assert unit["ProtectHome"] == "true"
        assert unit.get("PrivateTmp") == "true"
        writable = unit["ReadWritePaths"].split()
        assert "/var/lib/kb-advisor" in writable

    def test_memory_is_capped_for_a_1gb_box(self):
        unit = parse_unit()["Service"]
        assert unit["MemoryMax"].endswith(("M", "G"))
        assert unit["MemoryHigh"].endswith(("M", "G"))
        high = int(re.sub(r"\D", "", unit["MemoryHigh"]))
        max_ = int(re.sub(r"\D", "", unit["MemoryMax"]))
        assert high < max_, "MemoryHigh must sit below MemoryMax"

    def test_threads_are_budgeted_to_match_the_app_cap(self):
        unit = parse_unit()["Service"]
        assert int(unit["TasksMax"]) <= 256
        assert unit.get("NoNewPrivileges") == "true"

    def test_start_is_gated_on_health(self):
        unit = parse_unit()["Service"]
        assert unit["ExecStartPost"].endswith("wait_for_health.sh")


class TestUnitAndAppAgree:
    def test_writable_path_covers_the_log_dir_default(self):
        """ReadWritePaths is the silent killer: a miss drops every log row."""
        unit = parse_unit()["Service"]
        writable = unit["ReadWritePaths"].split()
        env = open(ENV_EXAMPLE).read()
        match = re.search(r"^#?\s*ADVISOR_LOG_DIR=(\S+)", env, re.MULTILINE)
        log_dir = match.group(1) if match else "/var/lib/kb-advisor/logs"
        assert any(log_dir.startswith(w) for w in writable), (
            "ADVISOR_LOG_DIR=%s is not under any ReadWritePaths %s"
            % (log_dir, writable))

    def test_service_name_matches_the_docs(self):
        unit = parse_unit()
        assert unit["Unit"]["Description"].startswith("KB Advisor")


class TestCaddy:
    def test_body_size_is_capped(self):
        text = open(CADDY).read()
        assert "request_body" in text and "max_size" in text

    def test_forwarded_for_is_replaced_not_appended(self):
        text = open(CADDY).read()
        assert "header_up X-Forwarded-For {remote_host}" in text, (
            "appending X-Forwarded-For lets a client mint a new rate-limit bucket")

    def test_security_headers_and_logging_present(self):
        text = open(CADDY).read()
        assert "Strict-Transport-Security" in text
        assert "X-Content-Type-Options" in text
        assert "roll_size" in text

    def test_tls_front_is_the_only_listener_in_the_example(self):
        text = open(CADDY).read()
        assert "reverse_proxy 127.0.0.1:8001" in text


class TestVersioning:
    def test_changelog_top_entry_matches_the_package_version(self):
        """Two places, one number: a release that disagrees with itself is a bug."""
        import re
        pyproject = open(os.path.join(ROOT, "pyproject.toml")).read()
        version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1)
        changelog = open(os.path.join(ROOT, "CHANGELOG.md")).read()
        headings = re.findall(r"^##\s+(\d+\.\d+\.\d+)", changelog, re.MULTILINE)
        assert headings, "CHANGELOG.md has no version headings"
        assert headings[0] == version, (
            "CHANGELOG top entry %s does not match pyproject %s" % (headings[0], version))

    def test_changelog_is_ordered_newest_first(self):
        import re
        changelog = open(os.path.join(ROOT, "CHANGELOG.md")).read()
        headings = re.findall(r"^##\s+(\d+)\.(\d+)\.(\d+)", changelog, re.MULTILINE)
        as_tuples = [tuple(int(p) for p in h) for h in headings]
        assert as_tuples == sorted(as_tuples, reverse=True), headings

    def test_python_floor_matches_ci(self):
        """The floor in pyproject and the versions CI runs have to agree."""
        import re
        pyproject = open(os.path.join(ROOT, "pyproject.toml")).read()
        floor = re.search(r'requires-python\s*=\s*">=([\d.]+)"', pyproject).group(1)
        workflow = open(os.path.join(ROOT, ".github", "workflows", "tests.yml")).read()
        tested = re.search(r'python-version:\s*\[([^\]]+)\]', workflow).group(1)
        assert floor in tested, (
            "pyproject claims >=%s but CI runs %s" % (floor, tested.strip()))


class TestEnvExample:
    def test_every_setting_module_reads_is_documented(self):
        """A knob nobody can find is a knob that gets left at the default."""
        env = open(ENV_EXAMPLE).read()
        for key in ("ADVISOR_TOKEN", "ADVISOR_PORT", "ADVISOR_MAX_THREADS",
                    "ADVISOR_REGEX_TIMEOUT_S", "ADVISOR_RATE_PER_MIN",
                    "ADVISOR_TRUST_PROXY", "ADVISOR_MAX_GENERATIONS",
                    "ADVISOR_LOG_MAX_BYTES", "ADVISOR_LOG_QUESTION",
                    "OPENROUTER_API_KEY"):
            assert key in env, "deploy/kb.env.example does not document %s" % key
