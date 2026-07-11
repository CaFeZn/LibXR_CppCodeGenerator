#!/usr/bin/env python3

import logging
import os
import sys
import subprocess
import argparse
import yaml
from typing import List

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

def is_stm32_project(path: str) -> bool:
    """Check if the given path contains any .ioc file."""
    try:
        return any(f.endswith(".ioc") for f in os.listdir(path))
    except Exception as e:
        logging.error(f"Cannot check directory '{path}': {e}")
        return False


def detect_platform(input_path: str) -> str:
    """Prefer the explicit YAML platform and keep .ioc detection for old files."""
    try:
        with open(input_path, "r", encoding="utf-8") as source:
            config = yaml.safe_load(source) or {}
        mcu = config.get("Mcu", {}) if isinstance(config, dict) else {}
        platform = str(mcu.get("Platform", mcu.get("Family", ""))).upper()
        if platform == "HPM":
            return "HPM"
        if platform.startswith("STM32"):
            return "STM32"
    except (OSError, yaml.YAMLError):
        pass
    return "STM32" if is_stm32_project(os.path.dirname(input_path)) else ""


def main():
    from libxr.PackageInfo import LibXRPackageInfo

    LibXRPackageInfo.check_and_print()
    
    parser = argparse.ArgumentParser(description="Wrapper for STM32 code generation.")
    parser.add_argument("-i", "--input", required=True,
                        help="Input YAML configuration file path")

    # We don't parse all args because we want to forward unknown ones later
    known_args, unknown_args = parser.parse_known_args()

    input_path = os.path.abspath(known_args.input)
    if not os.path.isfile(input_path):
        logging.error(f"YAML configuration file not found: {input_path}")
        sys.exit(1)

    platform = detect_platform(input_path)
    if not platform:
        logging.info("Skipped: Unsupported or unidentified project platform.")
        sys.exit(0)

    # Forward all original arguments (not just known) to the generator
    module = "libxr.GeneratorCodeHPM" if platform == "HPM" else "libxr.GeneratorCodeSTM32"
    cmd: List[str] = [sys.executable, "-m", module, *sys.argv[1:]]

    logging.info("%s project detected.", platform)
    logging.debug(f"CMD: {' '.join(cmd)}")

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        logging.error(f"Code generation failed with exit code {e.returncode}")
        sys.exit(e.returncode)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
