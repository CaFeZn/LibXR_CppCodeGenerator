"""Command-line interface for the HPM peripheral configuration backend."""

import argparse
import sys
from typing import Any, List, Optional, TextIO

import yaml

from .diagnostics import Diagnostic, ERROR
from .protocol import dumps_json, generate_envelope, inspect_envelope, validate_envelope
from .service import generate as generate_project
from .service import inspect, validate

COMMANDS = ("inspect", "validate", "generate")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xr_hpm_cfg",
        description="Inspect, validate, and generate LibXR HPM projects",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect an HPM project without changing files"
    )
    inspect_parser.add_argument(
        "-d", "--directory", required=True, help="HPM project directory"
    )
    inspect_parser.add_argument(
        "-i", "--input", default="", help="Explicit .hpmpc path"
    )
    inspect_parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format"
    )

    validate_parser = subparsers.add_parser(
        "validate", help="Normalize and validate an HPM peripheral configuration"
    )
    validate_parser.add_argument(
        "-d", "--directory", required=True, help="HPM project directory"
    )
    validate_parser.add_argument(
        "-i", "--input", default="", help="Explicit .hpmpc path"
    )
    validate_parser.add_argument(
        "--peripheral-config",
        default="hpm_peripherals.yaml",
        help="Peripheral configuration YAML path",
    )
    validate_parser.add_argument(
        "--config-stdin",
        action="store_true",
        help="Read an unsaved YAML or JSON configuration from stdin",
    )
    validate_parser.add_argument(
        "--write", action="store_true", help="Write the normalized configuration"
    )
    validate_parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format"
    )

    generate_parser = subparsers.add_parser(
        "generate", help="Validate and generate every managed HPM project file"
    )
    generate_parser.add_argument(
        "-d", "--directory", required=True, help="HPM project directory"
    )
    generate_parser.add_argument(
        "-i", "--input", default="", help="Explicit .hpmpc path"
    )
    generate_parser.add_argument(
        "--peripheral-config",
        default="hpm_peripherals.yaml",
        help="Peripheral configuration YAML path",
    )
    generate_parser.add_argument(
        "--config-stdin",
        action="store_true",
        help="Read an unsaved YAML or JSON configuration from stdin and commit it atomically",
    )
    generate_parser.add_argument(
        "--libxr-config",
        default="User/libxr_config.yaml",
        help="LibXR settings YAML path",
    )
    generate_parser.add_argument(
        "--config-output", default=".config.yaml", help="Parsed HPM YAML output"
    )
    generate_parser.add_argument(
        "-o", "--output", default="User/app_main.cpp", help="Output app_main.cpp"
    )
    generate_parser.add_argument(
        "--xrobot", action="store_true", help="Generate XRobot integration"
    )
    generate_parser.add_argument(
        "--hw-cntr", action="store_true", help="Generate HardwareContainer"
    )
    generate_parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="Output format"
    )
    return parser


def _write_inspect_text(result: dict, stream: TextIO) -> None:
    project = result["project"]
    stream.write(
        "{board} / {soc} / {hpmpc}\n".format(
            board=project["board"], soc=project["soc"], hpmpc=project["hpmpc"]
        )
    )
    stream.write("Pinmux functions: {}\n".format(len(result["pinmux_functions"])))
    stream.write("Peripherals: {}\n".format(len(result["peripherals"])))


def _inspect_error(error: Exception) -> dict:
    diagnostic = Diagnostic(
        "HPM_PROJECT_INSPECTION_FAILED",
        ERROR,
        str(error),
    )
    return inspect_envelope({}, [], [], errors=(diagnostic,))


def _validate_error(error: Exception) -> dict:
    diagnostic = Diagnostic(
        "HPM_CONFIG_VALIDATION_FAILED",
        ERROR,
        str(error),
    )
    return validate_envelope(False, {}, errors=(diagnostic,))


def _read_stdin_config(stream: TextIO) -> Any:
    value = yaml.safe_load(stream.read())
    if not isinstance(value, dict):
        raise ValueError("Configuration from stdin must be a mapping")
    return value


def _write_validate_text(result: dict, stream: TextIO) -> None:
    if result["valid"]:
        stream.write("Configuration is valid")
        if result["warnings"]:
            stream.write(" with {} warning(s)".format(len(result["warnings"])))
        stream.write(".\n")
        return
    stream.write(
        "Configuration is invalid with {} error(s).\n".format(len(result["errors"]))
    )
    for diagnostic in result["errors"]:
        stream.write("[{}] {}\n".format(diagnostic["code"], diagnostic["message"]))


def _write_generate_text(result: dict, stream: TextIO) -> None:
    if result["success"]:
        stream.write("Generated {} file(s).\n".format(len(result["generated_files"])))
        for path in result["generated_files"]:
            stream.write("  {}\n".format(path))
        return
    stream.write("Generation failed with {} error(s).\n".format(len(result["errors"])))
    for diagnostic in result["errors"]:
        stream.write("[{}] {}\n".format(diagnostic["code"], diagnostic["message"]))


def run(
    argv: Optional[List[str]] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    """Run the CLI and return an exit status without exiting the interpreter."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    input_stream = stdin or sys.stdin

    if not arguments or arguments[0] not in COMMANDS:
        from libxr.ConfigHPMProject import legacy_main

        return legacy_main(arguments)

    parser = _build_parser()
    args = parser.parse_args(arguments)
    if args.command == "inspect":
        try:
            result = inspect(args.directory, args.input)
        except (OSError, ValueError) as error:
            result = _inspect_error(error)
            if args.format == "json":
                output.write(dumps_json(result) + "\n")
            else:
                error_output.write("HPM project inspection failed: {}\n".format(error))
            return 1
        if args.format == "json":
            output.write(dumps_json(result) + "\n")
        else:
            _write_inspect_text(result, output)
        return 0

    if args.command == "validate":
        try:
            candidate = _read_stdin_config(input_stream) if args.config_stdin else None
            result = validate(
                args.directory,
                args.input,
                args.peripheral_config,
                candidate,
                args.write,
            )
        except (OSError, ValueError, yaml.YAMLError) as error:
            result = _validate_error(error)
        if args.format == "json":
            output.write(dumps_json(result) + "\n")
        else:
            _write_validate_text(result, output if result["valid"] else error_output)
        return 0 if result["valid"] else 1
    if args.command == "generate":
        try:
            candidate = _read_stdin_config(input_stream) if args.config_stdin else None
            result = generate_project(
                args.directory,
                args.input,
                args.peripheral_config,
                args.libxr_config,
                args.config_output,
                args.output,
                args.xrobot,
                args.hw_cntr,
                candidate,
            )
        except (ValueError, yaml.YAMLError) as error:
            diagnostic = Diagnostic(
                "HPM_PERIPHERAL_CONFIG_INVALID",
                ERROR,
                str(error),
            )
            result = generate_envelope(False, [], errors=(diagnostic,))
        except Exception as error:
            diagnostic = Diagnostic(
                "HPM_INTERNAL_ERROR",
                ERROR,
                "Internal HPM generator error ({}).".format(type(error).__name__),
            )
            result = generate_envelope(False, [], errors=(diagnostic,))
        if args.format == "json":
            output.write(dumps_json(result) + "\n")
        else:
            _write_generate_text(result, output if result["success"] else error_output)
        return 0 if result["success"] else 1

    parser.error("Unsupported command: {}".format(args.command))
    return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
