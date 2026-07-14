#!/usr/bin/env python3
"""Exercise the installed xr_hpm_cfg entry point against the golden fixture."""

import argparse
import json
import shutil
import subprocess
import tempfile

from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, List

PROTOCOL_VERSION = 1
EXPECTED_GENERATED_FILES = {
    ".config.yaml",
    "User/app_main.cpp",
    "User/app_main.h",
    "User/libxr_config.yaml",
    "boards/hpm5361evklite/board.c",
    "boards/hpm5361evklite/board.h",
    "boards/hpm5361evklite/pinmux.c",
    "boards/hpm5361evklite/pinmux.h",
}


def _run(executable: str, project: Path, arguments: List[str]) -> Dict[str, Any]:
    result = subprocess.run(
        [executable, *arguments, "--format", "json"],
        cwd=str(project),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "xr_hpm_cfg failed with exit code {}: {}".format(
                result.returncode, result.stderr.strip()
            )
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("xr_hpm_cfg stdout is not one JSON document") from error
    if not isinstance(payload, dict):
        raise RuntimeError("xr_hpm_cfg JSON root is not an object")
    return payload


def _check_protocol(payload: Dict[str, Any], installed_version: str) -> None:
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise AssertionError("unexpected HPM protocol version")
    if payload.get("generator_version") != installed_version:
        raise AssertionError(
            "generator_version {!r} does not match installed libxr {!r}".format(
                payload.get("generator_version"), installed_version
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()

    executable = shutil.which("xr_hpm_cfg")
    if executable is None:
        raise RuntimeError("installed xr_hpm_cfg console entry point was not found")
    installed_version = importlib_metadata.version("libxr")
    if installed_version != args.expected_version:
        raise AssertionError(
            "installed libxr is {!r}, expected {!r}".format(
                installed_version, args.expected_version
            )
        )

    fixture = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "hpm5361evklite"
        / "input"
    )
    with tempfile.TemporaryDirectory(prefix="libxr_hpm_wheel_") as temporary:
        project = Path(temporary) / "project"
        shutil.copytree(str(fixture), str(project))
        common = [
            "-d",
            ".",
            "-i",
            "boards/hpm5361evklite/pinmux.hpmpc",
        ]

        inspected = _run(executable, project, ["inspect", *common])
        _check_protocol(inspected, installed_version)
        if inspected.get("project", {}).get("board") != "hpm5361evklite":
            raise AssertionError("inspect returned the wrong HPM board")

        validated = _run(
            executable,
            project,
            ["validate", *common, "--peripheral-config", "hpm_peripherals.yaml"],
        )
        _check_protocol(validated, installed_version)
        if validated.get("valid") is not True:
            raise AssertionError("fixture validation failed")

        generated = _run(
            executable,
            project,
            [
                "generate",
                *common,
                "--peripheral-config",
                "hpm_peripherals.yaml",
                "--libxr-config",
                "User/libxr_config.yaml",
                "--config-output",
                ".config.yaml",
                "-o",
                "User/app_main.cpp",
                "--hw-cntr",
            ],
        )
        _check_protocol(generated, installed_version)
        if generated.get("success") is not True:
            raise AssertionError("fixture generation failed")
        generated_files = generated.get("generated_files")
        if not isinstance(generated_files, list):
            raise AssertionError("generate did not return generated_files")
        if set(generated_files) != EXPECTED_GENERATED_FILES:
            raise AssertionError("generate returned an unexpected file contract")
        for relative_path in generated_files:
            if not (project / relative_path).is_file():
                raise AssertionError(
                    "reported file does not exist: {}".format(relative_path)
                )

    print(
        "Installed HPM CLI smoke checks passed for libxr {}.".format(installed_version)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
