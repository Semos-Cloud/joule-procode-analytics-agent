"""Session schema for the text-to-SQL prompt.

There is no static file schema. Columns come from whatever the MCP reports
returned when this conversation started.
"""

from src.warehouse.store import schema_text

__all__ = ["schema_text"]
