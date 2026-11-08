from eurus.coding.workflow_tools import workflow_tools
from eurus.tools.base import Tool


def workflow_registry() -> list[Tool]:
    return workflow_tools()
