#!/usr/bin/env python3
"""End-to-end HPM configuration parser and LibXR code generator."""

import argparse
import logging
import os

from libxr.GeneratorCodeHPM import write_outputs
from libxr.PeripheralAnalyzerHPM import find_hpmpc_files, parse_hpmpc_file, save_to_yaml

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")


def legacy_main(argv=None) -> int:
    from libxr.PackageInfo import LibXRPackageInfo

    LibXRPackageInfo.check_and_print()
    parser = argparse.ArgumentParser(
        description="Parse an HPM project and generate LibXR application code"
    )
    parser.add_argument(
        "-d", "--directory", required=True, help="HPM project directory"
    )
    parser.add_argument("-i", "--input", help="Explicit .hpmpc path")
    parser.add_argument("-o", "--output", help="Output app_main.cpp")
    parser.add_argument("--config-output", help="Output parsed YAML")
    parser.add_argument("--board-header", help="Explicit board.h path")
    parser.add_argument(
        "--xrobot", action="store_true", help="Generate XRobot integration"
    )
    parser.add_argument(
        "--hw-cntr", action="store_true", help="Generate HardwareContainer"
    )
    parser.add_argument("--libxr-config", default="", help="LibXR settings YAML")
    args = parser.parse_args(argv)

    project_dir = os.path.abspath(args.directory)
    if not os.path.isdir(project_dir):
        logging.error("HPM project directory not found: %s", project_dir)
        return 1
    matches = (
        [os.path.abspath(args.input)]
        if args.input
        else list(find_hpmpc_files(project_dir))
    )
    if len(matches) != 1:
        logging.error("Expected exactly one .hpmpc file, found %d", len(matches))
        return 1

    config_output = args.config_output or os.path.join(project_dir, ".config.yaml")
    code_output = args.output or os.path.join(project_dir, "User", "app_main.cpp")
    try:
        project = parse_hpmpc_file(matches[0], args.board_header)
        save_to_yaml(project, config_output)
        write_outputs(
            project,
            code_output,
            args.libxr_config,
            use_xrobot=args.xrobot,
            use_hw_cntr=args.hw_cntr,
        )
    except (OSError, ValueError) as error:
        logging.error("HPM project generation failed: %s", error)
        return 1

    logging.info("Configuration exported to: %s", config_output)
    logging.info("LibXR code generated at: %s", code_output)
    return 0


def main() -> None:
    from libxr.platforms.hpm.cli import run

    raise SystemExit(run())


if __name__ == "__main__":
    main()
