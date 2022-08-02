#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

# pyre-strict

import configparser
import io
import os
import sys
import unittest
from pathlib import Path

import toml
import toml.decoder
from eden.test_support.temporary_directory import TemporaryDirectoryMixin
from eden.test_support.testcase import EdenTestCaseBase

from .. import config as config_mod, configutil, util
from ..config import EdenInstance
from ..configinterpolator import EdenConfigInterpolator
from ..configutil import EdenConfigParser, UnexpectedType


def get_toml_test_file_invalid() -> str:
    return """
[core thisIsNotAllowed]
"""


def get_toml_test_file_defaults() -> str:
    return """
[core]
systemIgnoreFile = "/etc/eden/gitignore"
ignoreFile = "/home/${USER}/.gitignore"

[clone]
default-revision = "master"

[rage]
reporter = 'pastry --title "eden rage from $(hostname)"'
"""


def get_toml_test_file_user_rc() -> str:
    return """
[core]
ignoreFile = "/home/${USER}/.gitignore-override"
edenDirectory = "/home/${USER}/.eden"

["telemetry"]
scribe-cat = "/usr/local/bin/scribe_cat"
"""


def get_toml_test_file_system_rc() -> str:
    return """
["telemetry"]
scribe-cat = "/bad/path/to/scribe_cat"
"""


class TomlConfigTest(EdenTestCaseBase):
    def setUp(self) -> None:
        super().setUp()
        self._user = "bob"
        self._state_dir = self.tmp_dir / ".eden"
        self._etc_eden_dir = self.tmp_dir / "etc/eden"
        self._config_d = self.tmp_dir / "etc/eden/config.d"
        self._home_dir = self.tmp_dir / "home" / self._user
        self._interpolate_dict = {
            "USER": self._user,
            "USER_ID": "42",
            "HOME": str(self._home_dir),
        }

        self._state_dir.mkdir()
        self._config_d.mkdir(exist_ok=True, parents=True)
        self._home_dir.mkdir(exist_ok=True, parents=True)

        self.unsetenv("EDEN_EXPERIMENTAL_SYSTEMD")

    def copy_config_files(self) -> None:
        path = self._config_d / "defaults.toml"
        path.write_text(get_toml_test_file_defaults())

        path = self._home_dir / ".edenrc"
        path.write_text(get_toml_test_file_user_rc())

        path = self._etc_eden_dir / "edenfs.rc"
        path.write_text(get_toml_test_file_system_rc())

    def assert_core_config(self, cfg: EdenInstance) -> None:
        self.assertEqual(
            cfg.get_config_value("rage.reporter", default=""),
            'pastry --title "eden rage from $(hostname)"',
        )
        self.assertEqual(
            cfg.get_config_value("core.ignoreFile", default=""),
            f"/home/{self._user}/.gitignore-override",
        )
        self.assertEqual(
            cfg.get_config_value("core.systemIgnoreFile", default=""),
            "/etc/eden/gitignore",
        )
        self.assertEqual(
            cfg.get_config_value("core.edenDirectory", default=""),
            f"/home/{self._user}/.eden",
        )

    def assert_config_precedence(self, cfg: EdenInstance) -> None:
        self.assertEqual(
            cfg.get_config_value("telemetry.scribe-cat", default=""),
            "/usr/local/bin/scribe_cat",
        )

    def test_load_config(self) -> None:
        self.copy_config_files()
        cfg = self.get_config()

        # Check the various config sections
        self.assert_core_config(cfg)
        self.assert_config_precedence(cfg)

        # Check if test is for toml or cfg by cfg._user_toml_cfg
        exp_rc_files = [
            self._config_d / "defaults.toml",
            self._etc_eden_dir / "edenfs.rc",
            self._home_dir / ".edenrc",
        ]
        self.assertEqual(cfg.get_rc_files(), exp_rc_files)

    def test_no_dot_edenrc(self) -> None:
        self.copy_config_files()

        (self._home_dir / ".edenrc").unlink()
        cfg = self.get_config()
        cfg._loadConfig()

        self.assertEqual(
            cfg.get_config_value("rage.reporter", default=""),
            'pastry --title "eden rage from $(hostname)"',
        )
        self.assertEqual(
            cfg.get_config_value("core.ignoreFile", default=""),
            f"/home/{self._user}/.gitignore",
        )
        self.assertEqual(
            cfg.get_config_value("core.systemIgnoreFile", default=""),
            "/etc/eden/gitignore",
        )

    def test_toml_error(self) -> None:
        self.copy_config_files()

        self.write_user_config(get_toml_test_file_invalid())

        cfg = self.get_config()
        with self.assertRaises(toml.decoder.TomlDecodeError):
            cfg._loadConfig()

    def test_get_config_value_returns_default_if_section_is_missing(self) -> None:
        self.assertEqual(
            self.get_config().get_config_value(
                "missing_section.test_option", default="test default"
            ),
            "test default",
        )

    def test_get_config_value_returns_default_if_option_is_missing(self) -> None:
        self.write_user_config(
            """[test_section]
other_option = "test value"
"""
        )
        self.assertEqual(
            self.get_config().get_config_value(
                "test_section.missing_option", default="test default"
            ),
            "test default",
        )

    def test_get_config_value_returns_value_for_string_option(self) -> None:
        self.write_user_config(
            """[test_section]
test_option = "test value"
"""
        )
        self.assertEqual(
            self.get_config().get_config_value(
                "test_section.test_option", default="test default"
            ),
            "test value",
        )

    def test_experimental_systemd_is_disabled_by_default(self) -> None:
        self.assertFalse(self.get_config().should_use_experimental_systemd_mode())

    def test_experimental_systemd_is_enabled_with_environment_variable(self) -> None:
        if not sys.platform.startswith("linux"):
            return

        self.setenv("EDEN_EXPERIMENTAL_SYSTEMD", "1")
        self.assertTrue(self.get_config().should_use_experimental_systemd_mode())

    def test_experimental_systemd_is_enabled_with_user_config_setting(self) -> None:
        if not sys.platform.startswith("linux"):
            return

        self.write_user_config(
            """[service]
experimental_systemd = true
"""
        )
        self.assertTrue(self.get_config().should_use_experimental_systemd_mode())

    def test_experimental_systemd_environment_variable_overrides_config(self) -> None:
        if not sys.platform.startswith("linux"):
            return

        self.setenv("EDEN_EXPERIMENTAL_SYSTEMD", "1")
        self.write_user_config("""[service]
experimental_systemd = false
""")
        self.assertTrue(self.get_config().should_use_experimental_systemd_mode())

        self.setenv("EDEN_EXPERIMENTAL_SYSTEMD", "0")
        self.write_user_config("""[service]
experimental_systemd = true
""")
        self.assertFalse(self.get_config().should_use_experimental_systemd_mode())

    def test_empty_experimental_systemd_environment_variable_does_not_override_config(
        self,
    ) -> None:
        if not sys.platform.startswith("linux"):
            return

        self.setenv("EDEN_EXPERIMENTAL_SYSTEMD", "")
        self.write_user_config("""[service]
experimental_systemd = true
""")
        self.assertTrue(self.get_config().should_use_experimental_systemd_mode())

        self.setenv("EDEN_EXPERIMENTAL_SYSTEMD", "")
        self.write_user_config("""[service]
experimental_systemd = false
""")
        self.assertFalse(self.get_config().should_use_experimental_systemd_mode())

    def test_user_id_variable_is_set_to_process_uid(self) -> None:
        config = self.get_config_without_stub_variables()
        self.write_user_config(
            """
[testsection]
testoption = "My user ID is ${USER_ID}."
"""
        )
        self.assertEqual(
            config.get_config_value("testsection.testoption", default=""),
            f"My user ID is {os.getuid()}.",
        )

    def test_default_fallback_systemd_xdg_runtime_dir_is_run_user_uid(self) -> None:
        self.assertEqual(
            self.get_config().get_fallback_systemd_xdg_runtime_dir(), "/run/user/42"
        )

    def test_configured_fallback_systemd_xdg_runtime_dir_expands_user_and_user_id(
        self,
    ) -> None:
        self.write_user_config(
            """
[service]
fallback_systemd_xdg_runtime_dir = "/var/run/${USER}/${USER_ID}"
"""
        )
        self.assertEqual(
            self.get_config().get_fallback_systemd_xdg_runtime_dir(), "/var/run/bob/42"
        )

    def test_printed_config_is_valid_toml(self) -> None:
        self.write_user_config(
            """
[clone]
default-revision = "master"
"""
        )

        printed_config = io.BytesIO()
        self.get_config().print_full_config(printed_config)
        parsed_config = printed_config.getvalue().decode("utf-8")
        parsed_toml = toml.loads(parsed_config)

        self.assertIn("clone", parsed_toml)
        self.assertEqual(parsed_toml["clone"].get("default-revision"), "master")

    def test_printed_config_expands_variables(self) -> None:
        self.write_user_config(
            """
["repository fbsource"]
type = "hg"
path = "/data/users/${USER}/fbsource"
"""
        )

        printed_config = io.BytesIO()
        self.get_config().print_full_config(printed_config)

        self.assertIn(b"/data/users/bob/fbsource", printed_config.getvalue())

    def test_printed_config_writes_booleans_as_booleans(self) -> None:
        self.write_user_config(
            """
[service]
experimental_systemd = true
"""
        )

        printed_config = io.BytesIO()
        self.get_config().print_full_config(printed_config)
        parsed_config = printed_config.getvalue().decode("utf-8")

        self.assertRegex(parsed_config, r"experimental_systemd\s*=\s*true")

    def get_config(self) -> EdenInstance:
        return EdenInstance(
            self._state_dir, self._etc_eden_dir, self._home_dir, self._interpolate_dict
        )

    def get_config_without_stub_variables(self) -> EdenInstance:
        return EdenInstance(
            self._state_dir, self._etc_eden_dir, self._home_dir, interpolate_dict=None
        )

    def write_user_config(self, content: str) -> None:
        (self._home_dir / ".edenrc").write_text(content)


class EdenConfigParserTest(unittest.TestCase):
    unsupported_value = {"dict of string to string": ""}

    def test_loading_config_with_unsupported_type_is_not_an_error(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"test_option": self.unsupported_value}})

    def test_querying_bool_returns_bool(self) -> None:
        for value in [True, False]:
            with self.subTest(value=value):
                parser = EdenConfigParser()
                parser.read_dict({"test_section": {"test_option": value}})
                self.assertEqual(
                    parser.get_bool("test_section", "test_option", default=True), value
                )
                self.assertEqual(
                    parser.get_bool("test_section", "test_option", default=False), value
                )

    def test_querying_bool_with_non_boolean_value_fails(self) -> None:
        for value in ["not a boolean", "", "true", "True", 0]:
            with self.subTest(value=value):
                parser = EdenConfigParser()
                parser.read_dict({"test_section": {"test_option": value}})
                with self.assertRaises(UnexpectedType) as expectation:
                    parser.get_bool("test_section", "test_option", default=False)
                self.assertEqual(expectation.exception.section, "test_section")
                self.assertEqual(expectation.exception.option, "test_option")
                self.assertEqual(expectation.exception.value, value)
                self.assertEqual(expectation.exception.expected_type, bool)

    def test_querying_bool_with_value_of_unsupported_type_fails(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"test_option": self.unsupported_value}})
        with self.assertRaises(UnexpectedType) as expectation:
            parser.get_bool("test_section", "test_option", default=False)
        self.assertEqual(expectation.exception.section, "test_section")
        self.assertEqual(expectation.exception.option, "test_option")
        self.assertEqual(expectation.exception.value, self.unsupported_value)
        self.assertEqual(expectation.exception.expected_type, bool)

    def test_querying_str_with_non_string_value_fails(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"test_option": True}})
        with self.assertRaises(UnexpectedType) as expectation:
            parser.get_str("test_section", "test_option", default="")
        self.assertEqual(expectation.exception.section, "test_section")
        self.assertEqual(expectation.exception.option, "test_option")
        self.assertEqual(expectation.exception.value, True)
        self.assertEqual(expectation.exception.expected_type, str)

    def test_querying_section_str_to_str_returns_mapping(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"a": "a value", "b": "b value"}})
        section = parser.get_section_str_to_str("test_section")
        self.assertCountEqual(section, {"a", "b"})
        self.assertEqual(section["a"], "a value")
        self.assertEqual(section["b"], "b value")

    def test_querying_section_str_to_any_fails_if_option_has_unsupported_type(
        self,
    ) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"unsupported": self.unsupported_value}})
        with self.assertRaises(UnexpectedType) as expectation:
            parser.get_section_str_to_any("test_section")
        self.assertEqual(expectation.exception.section, "test_section")
        self.assertEqual(expectation.exception.option, "unsupported")
        self.assertEqual(expectation.exception.value, self.unsupported_value)
        self.assertIsNone(expectation.exception.expected_type)

    def test_querying_section_str_to_any_interpolates_options(self) -> None:
        parser = EdenConfigParser(
            interpolation=EdenConfigInterpolator({"USER": "alice"})
        )
        parser.read_dict({"test_section": {"test_option": "hello ${USER}"}})
        section = parser.get_section_str_to_any("test_section")
        self.assertEqual(section.get("test_option"), "hello alice")

    def test_querying_section_str_to_any_returns_any_supported_type(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict(
            {
                "test_section": {
                    "bool_option": True,
                    "string_array_option": ["hello", "world"],
                    "string_option": "hello",
                }
            }
        )
        section = parser.get_section_str_to_any("test_section")
        self.assertEqual(section["bool_option"], True)
        self.assertEqual(list(section["string_array_option"]), ["hello", "world"])
        self.assertEqual(section["string_option"], "hello")

    def test_querying_section_str_to_str_with_non_string_value_fails(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"a": False}})
        with self.assertRaises(UnexpectedType) as expectation:
            parser.get_section_str_to_str("test_section")
        self.assertEqual(expectation.exception.section, "test_section")
        self.assertEqual(expectation.exception.option, "a")
        self.assertEqual(expectation.exception.value, False)
        self.assertEqual(expectation.exception.expected_type, str)

    def test_querying_section_str_to_str_of_missing_section_fails(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"a": "a value"}})
        with self.assertRaises(configparser.NoSectionError) as expectation:
            parser.get_section_str_to_str("not_test_section")
        section: str = expectation.exception.section
        self.assertEqual(section, "not_test_section")

    def test_querying_strs_with_empty_array_returns_empty_sequence(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"test_option": []}})
        self.assertEqual(
            list(
                parser.get_strs(
                    "test_section", "test_option", default=["default value"]
                )
            ),
            [],
        )

    def test_querying_strs_with_array_of_strings_returns_strs(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"test_option": ["first", "second", "3rd"]}})
        self.assertEqual(
            list(parser.get_strs("test_section", "test_option", default=[])),
            ["first", "second", "3rd"],
        )

    def test_querying_strs_with_array_of_non_strings_fails(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"test_option": [123]}})
        with self.assertRaises(UnexpectedType) as expectation:
            parser.get_strs("test_section", "test_option", default=[])
        self.assertEqual(expectation.exception.section, "test_section")
        self.assertEqual(expectation.exception.option, "test_option")
        self.assertEqual(expectation.exception.value, [123])
        self.assertEqual(expectation.exception.expected_type, configutil.Strs)

    def test_querying_missing_value_as_strs_returns_default(self) -> None:
        parser = EdenConfigParser()
        parser.read_dict({"test_section": {"bogus_option": []}})
        self.assertEqual(
            list(
                parser.get_strs(
                    "test_section", "missing_option", default=["default value"]
                )
            ),
            ["default value"],
        )

    def test_str_sequences_are_interpolated(self) -> None:
        parser = EdenConfigParser(
            interpolation=EdenConfigInterpolator({"USER": "alice"})
        )
        parser.read_dict(
            {
                "test_section": {
                    "test_option": ["sudo", "-u", "${USER}", "echo", "Hello, ${USER}!"]
                }
            }
        )
        self.assertEqual(
            list(parser.get_strs("test_section", "test_option", default=[])),
            ["sudo", "-u", "alice", "echo", "Hello, alice!"],
        )

    def test_unexpected_type_error_messages_are_helpful(self) -> None:
        self.assertEqual(
            'Expected boolean for service.experimental_systemd, but got string: "true"',
            str(
                UnexpectedType(
                    section="service",
                    option="experimental_systemd",
                    value="true",
                    expected_type=bool,
                )
            ),
        )

        self.assertEqual(
            "Expected string for repository myrepo.path, but got boolean: true",
            str(
                UnexpectedType(
                    section="repository myrepo",
                    option="path",
                    value=True,
                    expected_type=str,
                )
            ),
        )

        self.assertRegex(
            str(
                UnexpectedType(
                    section="section", option="option", value={}, expected_type=None
                )
            ),
            r"^Unexpected dict for section.option: \{\s*\}$",
        )

        self.assertEqual(
            "Expected array of strings for service.command, but got array: [ 123,]",
            str(
                UnexpectedType(
                    section="service",
                    option="command",
                    value=[123],
                    expected_type=configutil.Strs,
                )
            ),
        )


class EdenInstanceConstructionTest(unittest.TestCase):
    def test_full_cmd_line(self) -> None:
        cmdline = [
            b"/usr/local/libexec/eden/edenfs",
            b"--edenfs",
            b"--edenDir",
            b"/data/users/testuser/.eden",
            b"--etcEdenDir",
            b"/etc/eden",
            b"--configPath",
            b"/home/testuser/.edenrc",
            b"--edenfsctlPath",
            b"/usr/local/bin/edenfsctl",
            b"--takeover",
            b"",
        ]
        instance = config_mod.eden_instance_from_cmdline(cmdline)
        self.assertEqual(instance.state_dir, Path("/data/users/testuser/.eden"))
        self.assertEqual(instance.etc_eden_dir, Path("/etc/eden"))
        self.assertEqual(instance.home_dir, Path("/home/testuser/"))

    def test_sparse_cmd_line(self) -> None:
        cmdline = [
            b"/usr/local/libexec/eden/edenfs",
            b"--edenfs",
            b"--etcEdenDir",
            b"/etc/eden",
            b"--configPath",
            b"/home/testuser/.edenrc",
            b"--edenfsctlPath",
            b"/usr/local/bin/edenfsctl",
            b"--takeover",
            b"",
        ]
        instance = config_mod.eden_instance_from_cmdline(cmdline)

        self.assertEqual(
            instance.state_dir, Path("/home/testuser/local/.eden").resolve()
        )
        self.assertEqual(instance.etc_eden_dir, Path("/etc/eden"))
        self.assertEqual(instance.home_dir, Path("/home/testuser/"))

    def test_malformed_cmd_line(self) -> None:
        cmdline = [
            b"/usr/local/libexec/eden/edenfs",
            b"--configPath",
            b"/home/testuser/.edenrc",
        ]
        instance = config_mod.eden_instance_from_cmdline(cmdline)

        self.assertEqual(
            instance.state_dir, Path("/home/testuser/local/.eden").resolve()
        )
        self.assertEqual(instance.etc_eden_dir, Path("/etc/eden"))
        self.assertEqual(instance.home_dir, Path("/home/testuser/"))
