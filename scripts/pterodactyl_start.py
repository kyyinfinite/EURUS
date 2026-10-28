import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = [
    ROOT / "pyproject.toml",
    ROOT / "src" / "eurus" / "__init__.py",
]


def stop(message, code=1):
    print(f"ERROR: {message}", flush=True)
    raise SystemExit(code)


def run(command, label):
    print(f"\n==> {label}", flush=True)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
    )
    if result.returncode != 0:
        stop(f"{label} failed with exit code {result.returncode}.", result.returncode)


def main():
    os.chdir(ROOT)

    print("=" * 45, flush=True)
    print("EURUS | PTERODACTYL BOOTSTRAP", flush=True)
    print("=" * 45, flush=True)

    for path in REQUIRED_FILES:
        if not path.is_file():
            stop(f"Source file not found: {path}")

    bot_token = os.environ.get("EURUS_TELEGRAM_BOT_TOKEN", "").strip()
    allowed_users = os.environ.get("EURUS_TELEGRAM_ALLOWED_USER_IDS", "").strip()

    if not bot_token:
        stop(
            "EURUS_TELEGRAM_BOT_TOKEN is not configured. Set it in the Startup "
            "Variables or the environment before starting."
        )

    try:
        user_ids = [int(value.strip()) for value in allowed_users.split(",") if value.strip()]
    except ValueError:
        stop("EURUS_TELEGRAM_ALLOWED_USER_IDS must contain numeric IDs.")

    if not user_ids or any(uid <= 0 for uid in user_ids):
        stop("At least one positive Telegram user ID must be configured.")

    os.environ.setdefault("EURUS_HOME", str(ROOT / ".eurus"))

    print(f"Python: {sys.version.split()[0]}", flush=True)
    print(f"Directory: {ROOT}", flush=True)
    print(f"Authorized Telegram users: {len(user_ids)}", flush=True)

    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-e",
            ".[telegram]",
        ],
        "Installing EURUS dependencies",
    )

    print("\n==> Starting Telegram gateway", flush=True)

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "eurus",
                "gateway",
                "telegram",
            ],
            cwd=ROOT,
            env=os.environ.copy(),
        )
    except KeyboardInterrupt:
        print("\nThe EURUS gateway was stopped.", flush=True)
        return

    if result.returncode != 0:
        stop(
            f"The gateway exited with code {result.returncode}. Check the EURUS CLI logs above.",
            result.returncode,
        )


if __name__ == "__main__":
    main()
