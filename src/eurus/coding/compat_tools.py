from eurus.coding.workflow_tools import workflow_tools
from eurus.tools.base import Tool


def compat_tools() -> list[Tool]:
    return workflow_tools()
