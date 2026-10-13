"""
File type detection for downloaded planning documents.
"""

from pathlib import Path

# Extension -> document type
TYPE_MAP = {
    '.pdf': 'pdf',
    '.docx': 'docx',
    '.doc': 'doc',
    '.rtf': 'rtf',
    '.txt': 'txt',
    '.odt': 'odt',
    '.html': 'html',
    '.htm': 'html',
}

# Tier 1 formats, extracted by default
DEFAULT_SUPPORTED_TYPES = ['pdf', 'docx', 'rtf', 'txt', 'html']


def detect_file_type(file_path: Path) -> str:
    """
    Detect document type from extension.

    Returns:
        'pdf' | 'docx' | 'rtf' | 'txt' | 'html' | 'doc' | 'odt' | 'unknown'
    """
    return TYPE_MAP.get(file_path.suffix.lower(), 'unknown')


def is_supported_type(file_type: str, supported_types: list) -> bool:
    """Check whether a detected type is in the list of types to extract."""
    return file_type in supported_types
