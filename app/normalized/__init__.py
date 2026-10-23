"""NormalizedDocument v1: the machine contract for a converted document.

Markdown is for people. Another system needs the same content with every
piece of it traceable to a page and a box on that page, which Markdown throws
away. See ``schemas/normalized-document.v1.schema.json``.
"""

from app.normalized.builder import (
    CONVERTER_NAME,
    CONVERTER_VERSION,
    SCHEMA_VERSION,
    build,
    normalize,
)

__all__ = ["CONVERTER_NAME", "CONVERTER_VERSION", "SCHEMA_VERSION", "build", "normalize"]
