import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from libxr.platforms.hpm import cli, service
from libxr.platforms.hpm.markers import MARKER_BEGIN

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "hpm5361evklite" / "input"


def snapshot_files(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class HpmGenerationSafetyTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "project"
        shutil.copytree(FIXTURE_ROOT, self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _arguments(self, **overrides):
        values = {
            "libxr_config": "User/libxr_config.yaml",
            "config_output": ".config.yaml",
            "app_output": "User/app_main.cpp",
        }
        values.update(overrides)
        return [
            "generate",
            "-d",
            str(self.root),
            "-i",
            "boards/hpm5361evklite/pinmux.hpmpc",
            "--peripheral-config",
            "hpm_peripherals.yaml",
            "--libxr-config",
            values["libxr_config"],
            "--config-output",
            values["config_output"],
            "-o",
            values["app_output"],
            "--hw-cntr",
            "--format",
            "json",
        ]

    def _invoke(self, arguments=None, stdin=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = cli.run(
            self._arguments() if arguments is None else arguments,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        serialized = stdout.getvalue()
        payload = json.loads(serialized)
        self.assertEqual(serialized.count("\n"), 1)
        return exit_code, payload, serialized, stderr.getvalue()

    def _assert_failure(self, expected_code, before, arguments=None, stdin=None):
        exit_code, payload, stdout, stderr = self._invoke(arguments, stdin=stdin)
        self.assertNotEqual(exit_code, 0, (stdout, stderr))
        self.assertFalse(payload["success"])
        self.assertEqual(payload["generated_files"], [])
        self.assertIn(expected_code, {item["code"] for item in payload["errors"]})
        self.assertEqual(snapshot_files(self.root), before)
        return payload

    def _stdin_generate_arguments(self):
        arguments = self._arguments()
        arguments.insert(arguments.index("--format"), "--config-stdin")
        return arguments

    def test_stdin_candidate_is_committed_with_all_generated_files(self):
        config_path = self.root / "hpm_peripherals.yaml"
        candidate = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        candidate["uart"]["UART3"]["baudrate"] = 460_800

        exit_code, payload, stdout, stderr = self._invoke(
            self._stdin_generate_arguments(),
            stdin=io.StringIO(json.dumps(candidate)),
        )

        self.assertEqual(exit_code, 0, (stdout, stderr))
        self.assertTrue(payload["success"])
        self.assertIn("hpm_peripherals.yaml", payload["generated_files"])
        persisted = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["uart"]["UART3"]["baudrate"], 460_800)

    def test_stdin_candidate_is_not_saved_when_rendering_fails(self):
        config_path = self.root / "hpm_peripherals.yaml"
        candidate = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        candidate["uart"]["UART3"]["baudrate"] = 460_800
        board = self.root / "boards" / "hpm5361evklite" / "board.c"
        board.write_text(
            board.read_text(encoding="utf-8").replace(
                "board_write_spi_cs", "missing_spi_cs_helper"
            ),
            encoding="utf-8",
        )
        before = snapshot_files(self.root)

        self._assert_failure(
            "HPM_BOARD_HELPER_MISSING",
            before,
            self._stdin_generate_arguments(),
            stdin=io.StringIO(json.dumps(candidate)),
        )

    def test_relative_output_escape_is_rejected_without_writes(self):
        outside = self.parent / "outside.yaml"
        before = snapshot_files(self.root)

        self._assert_failure(
            "HPM_OUTPUT_PATH_OUTSIDE_PROJECT",
            before,
            self._arguments(config_output="../outside.yaml"),
        )

        self.assertFalse(outside.exists())

    def test_absolute_output_escape_is_rejected_without_writes(self):
        outside = self.parent / "absolute-outside.yaml"
        before = snapshot_files(self.root)

        self._assert_failure(
            "HPM_OUTPUT_PATH_OUTSIDE_PROJECT",
            before,
            self._arguments(config_output=str(outside)),
        )

        self.assertFalse(outside.exists())

    def test_output_collisions_are_rejected_without_writes(self):
        board_c = "boards/hpm5361evklite/board.c"
        hpmpc = "boards/hpm5361evklite/pinmux.hpmpc"
        cases = {
            "config_app": {"config_output": "User/app_main.cpp"},
            "libxr_app": {"libxr_config": "User/app_main.cpp"},
            "libxr_config": {"config_output": "User/libxr_config.yaml"},
            "app_board": {"app_output": board_c},
            "config_hpmpc": {"config_output": hpmpc},
        }
        for name, overrides in cases.items():
            with self.subTest(name=name):
                before = snapshot_files(self.root)
                self._assert_failure(
                    "HPM_OUTPUT_PATH_CONFLICT",
                    before,
                    self._arguments(**overrides),
                )

    def test_third_replace_failure_rolls_back_the_entire_tree(self):
        before = snapshot_files(self.root)
        real_replace = os.replace
        replace_count = 0

        def fail_third(source, target):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 3:
                raise OSError("injected third replace failure")
            return real_replace(source, target)

        with mock.patch.object(service.os, "replace", side_effect=fail_third):
            self._assert_failure("HPM_GENERATION_COMMIT_FAILED", before)

        self.assertGreaterEqual(replace_count, 3)

    def test_unexpected_generate_exception_still_returns_internal_error_json(self):
        before = snapshot_files(self.root)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with mock.patch.object(
            cli, "generate_project", side_effect=RuntimeError("injected internal error")
        ):
            exit_code = cli.run(self._arguments(), stdout=stdout, stderr=stderr)

        serialized = stdout.getvalue()
        payload = json.loads(serialized)
        self.assertEqual(serialized.count("\n"), 1)
        self.assertNotEqual(exit_code, 0)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["generated_files"], [])
        self.assertEqual(payload["errors"][0]["code"], "HPM_INTERNAL_ERROR")
        self.assertNotIn("Traceback", serialized)
        self.assertEqual(snapshot_files(self.root), before)

    def test_marker_conflict_has_a_stable_code_and_writes_nothing(self):
        board = self.root / "boards" / "hpm5361evklite" / "board.c"
        board.write_text(
            board.read_text(encoding="utf-8") + "\n" + MARKER_BEGIN + "\n",
            encoding="utf-8",
        )
        before = snapshot_files(self.root)

        self._assert_failure("HPM_BOARD_MARKER_CONFLICT", before)

    def test_existing_crlf_app_main_remains_crlf_after_generation(self):
        app_main = self.root / "User" / "app_main.cpp"
        original = app_main.read_text(encoding="utf-8")
        app_main.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))

        exit_code, payload, stdout, stderr = self._invoke()

        self.assertEqual(exit_code, 0, (stdout, stderr))
        self.assertTrue(payload["success"])
        generated = app_main.read_bytes()
        self.assertIn(b"\r\n", generated)
        self.assertNotIn(b"\n", generated.replace(b"\r\n", b""))


if __name__ == "__main__":
    unittest.main()
