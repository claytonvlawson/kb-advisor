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
    # interpolation=None: systemd uses %i as an instance specifier, and
    # configparser would otherwise fail on '%i.env'
    parser = configparser.ConfigParser(strict=False, interpolation=None)
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


class TestMultiClientTopology:
    """One process per client is the supported topology; the unit is templated."""

    TEMPLATE = os.path.join(ROOT, "deploy", "kb-advisor@.service")
    SLICE = os.path.join(ROOT, "deploy", "kb-advisor.slice")

    def parse(self, path):
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        with open(path) as fh:
            parser.read_file(fh)
        return parser

    def test_template_unit_exists_and_is_instance_parameterised(self):
        assert os.path.exists(self.TEMPLATE), "no templated unit for N clients"
        unit = self.parse(self.TEMPLATE)["Service"]
        assert "%i" in unit["EnvironmentFile"], "the env file must follow the instance name"
        assert "%i" in unit["ReadWritePaths"], "each instance needs its own writable log dir"

    def test_template_keeps_the_same_hardening(self):
        unit = self.parse(self.TEMPLATE)["Service"]
        for key in ("NoNewPrivileges", "PrivateTmp", "PrivateDevices",
                    "ProtectSystem", "ProtectHome", "RestrictNamespaces"):
            assert unit.get(key) not in (None, "no", "false"), key
        assert unit["Restart"] == "always"
        assert unit["ExecStartPost"].endswith("wait_for_health.sh")

    def test_both_units_are_inside_the_bounded_slice(self):
        """A per-unit MemoryMax does not bound the sum of all instances."""
        for path in (UNIT, self.TEMPLATE):
            unit = self.parse(path)["Service"]
            assert unit.get("Slice") == "kb-advisor.slice", path
        assert os.path.exists(self.SLICE), "no slice means no collective ceiling"

    def test_slice_caps_the_total_not_just_each_instance(self):
        slice_cfg = self.parse(self.SLICE)["Slice"]
        total = int(re.sub(r"\D", "", slice_cfg["MemoryMax"]))
        per_unit = int(re.sub(r"\D", "", self.parse(UNIT)["Service"]["MemoryMax"]))
        assert total < per_unit * 2, (
            "the slice (%s) must be tighter than two full instances (%s each)"
            % (slice_cfg["MemoryMax"], per_unit))

    def test_readme_states_the_topology_and_the_non_goal(self):
        readme = open(os.path.join(ROOT, "README.md")).read()
        assert "One process per client" in readme
        assert "kb-advisor@" in readme, "the templated unit is not documented"
        assert "not built" in readme, "the multi-tenant non-goal is not stated"
        assert "300 MB" in readme, "the memory arithmetic is not stated"


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


class TestPinning:
    """The reproducibility claim is only as good as the pins behind it."""

    def test_constraints_pins_every_runtime_dependency(self):
        import re
        runtime = open(os.path.join(ROOT, "requirements.txt")).read()
        # names declared in requirements.txt, ignoring comments and extras
        names = set()
        for line in runtime.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.add(re.split(r"[<>=!\[]", line)[0].lower().replace("_", "-"))
        constraints = open(os.path.join(ROOT, "constraints.txt")).read()
        pinned = {}
        for line in constraints.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, version = line.partition("==")
            pinned[name.lower().replace("_", "-")] = version
        missing = names - set(pinned)
        assert not missing, "unpinned runtime dependencies: %s" % sorted(missing)
        for name, version in pinned.items():
            assert version and not version.startswith("<"), (
                "%s must be pinned to an exact version" % name)

    def test_ci_installs_against_the_constraints(self):
        workflow = open(os.path.join(ROOT, ".github", "workflows", "tests.yml")).read()
        assert "-c constraints.txt" in workflow, (
            "CI does not install with constraints.txt, so the pins are decorative")

    def test_ingest_extra_is_pinned_too(self):
        constraints = open(os.path.join(ROOT, "constraints.txt")).read().lower()
        assert "pymupdf==" in constraints, "the ingest dependency is not pinned"


class TestCommunityFiles:
    def test_security_policy_names_a_private_channel(self):
        text = open(os.path.join(ROOT, "SECURITY.md")).read()
        assert "private vulnerability reporting" in text.lower()
        assert "Do not open a public issue" in text

    def test_security_policy_is_specific_about_this_system(self):
        text = open(os.path.join(ROOT, "SECURITY.md")).read().lower()
        for topic in ("auth bypass", "crisis", "denial of service",
                      "cross-client", "logs"):
            assert topic in text, "SECURITY.md says nothing about %s" % topic

    def test_contributing_states_the_gates_and_the_invariants(self):
        text = open(os.path.join(ROOT, "CONTRIBUTING.md")).read()
        assert "crisis-eval" in text and "quality" in text
        assert "eval/crisis_set.jsonl" in text
        assert "one process per client" in text.lower()


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
