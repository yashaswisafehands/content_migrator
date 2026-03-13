"""Utility helpers for managing data directory paths."""

from pathlib import Path

# Base repository directory (module parent)
BASE_DIR = Path(__file__).resolve().parent

# Structured data directories (no legacy fallbacks)
DATA_DIR = BASE_DIR / "data"
CONFIGS_DIR = DATA_DIR / "configs"
MAPPINGS_DIR = DATA_DIR / "mappings"
PROCESSED_DATA_DIR = DATA_DIR / "processed_data"
TEMP_DATA_DIR = DATA_DIR / "temp_data"


def ensure_parent_dir(path: Path) -> None:
    """Ensure the parent directory for the given path exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def get_config_file(filename: str) -> Path:
    """Resolve the path for a configuration file (read-only)."""
    return CONFIGS_DIR / filename


def get_processed_file(filename: str) -> Path:
    """Resolve the path for a processed-data file (read/write)."""
    return PROCESSED_DATA_DIR / filename


def get_mappings_file(filename: str) -> Path:
    """Resolve the path for a mappings file (read/write)."""
    return MAPPINGS_DIR / filename


def get_temp_directory() -> Path:
    """Return the directory used for temporary markdown files."""
    return TEMP_DATA_DIR
