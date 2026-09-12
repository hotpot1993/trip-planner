"""存储层：SQLite 连接、表结构与检索。"""

from .connection import connect, transaction
from .schema import SCHEMA_VERSION, initialize
from .search import SourceMatch, document_contains, find_documents_containing

__all__ = [
    "SCHEMA_VERSION",
    "SourceMatch",
    "connect",
    "document_contains",
    "find_documents_containing",
    "initialize",
    "transaction",
]
