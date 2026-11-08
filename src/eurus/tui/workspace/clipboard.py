from __future__ import annotations

import base64
import shutil
import subprocess
from typing import Protocol

MAX_OSC52_BYTES = 76800


def osc52_sequence(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    if len(payload) > MAX_OSC52_BYTES:
        payload = payload[:MAX_OSC52_BYTES]
    return f"\x1b]52;c;{payload}\x07"


class TextOutput(Protocol):
    def write(self, data: str) -> object: ...

    def flush(self) -> object: ...


def copy_to_terminal_clipboard(text: str, output: TextOutput) -> bool:
    if not text:
        return False
    try:
        output.write(osc52_sequence(text))
        output.flush()
        return True
    except Exception:
        return False


SYSTEM_COMMANDS = {
    "darwin": ["/usr/bin/pbcopy"],
    "linux": ["wl-copy", "xclip", "xsel"],
    "win32": ["clip"],
}

TERMUX_COMMAND = "termux-clipboard-set"


def copy_with_system_commands(text: str) -> bool:
    import sys

    if not text:
        return False
    candidates = list(SYSTEM_COMMANDS.get(sys.platform, []))
    if shutil.which(TERMUX_COMMAND):
        candidates.append(TERMUX_COMMAND)
    for name in candidates:
        path = shutil.which(name)
        if path is None:
            continue
        try:
            result = subprocess.run(
                [path],
                input=text.encode("utf-8"),
                timeout=1.5,
                check=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return result.returncode == 0
    return False
