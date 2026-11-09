from eurus.bootstrap.detection.architecture import inspect_architecture, normalize_machine
from eurus.bootstrap.detection.capabilities import inspect_capabilities, inspect_environment
from eurus.bootstrap.detection.package_manager import chromium_install_hint, detect_package_manager
from eurus.bootstrap.detection.platform import inspect_platform
from eurus.bootstrap.detection.python import detect_venv, inspect_python

__all__ = [
    "chromium_install_hint",
    "detect_package_manager",
    "detect_venv",
    "inspect_architecture",
    "inspect_capabilities",
    "inspect_environment",
    "inspect_platform",
    "inspect_python",
    "normalize_machine",
]
