from eurus.bootstrap.startup.manager import (
    SERVICE_NAMES,
    detect_mechanisms,
    install_cron,
    install_launchers,
    install_systemd,
    install_termux_boot,
    recommend_backend,
    service_command,
    status_all,
    uninstall,
    uninstall_all,
)

__all__ = [
    "SERVICE_NAMES",
    "detect_mechanisms",
    "install_cron",
    "install_launchers",
    "install_systemd",
    "install_termux_boot",
    "recommend_backend",
    "service_command",
    "status_all",
    "uninstall",
    "uninstall_all",
]
