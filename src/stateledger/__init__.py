"""StateLedger ledger core.

M1: validated checkpoint contract and an append-only SQLite store.
M2: read-only Git capture, SHA-256 scope hashing, and freshness classification.

No MCP server, CLI, context pack, or frontend yet.
"""

from .models import SCHEMA_VERSION

__version__ = "0.1.0"
__all__ = ["SCHEMA_VERSION", "__version__"]
