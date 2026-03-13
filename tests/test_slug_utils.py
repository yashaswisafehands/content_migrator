import os
import sys

# Ensure tests can import local module when run from workspace root
CURRENT_DIR = os.path.dirname(__file__)
PARENT_DIR = os.path.abspath(os.path.join(CURRENT_DIR, os.pardir))
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

from slug_utils import merge_slug_parts, slugify  # noqa: E402


def test_slugify_special_characters_and_accents():
    assert slugify("Café con Leche & Té") == "cafe-con-leche-te"


def test_slugify_trailing_number_block_removed():
    assert slugify("module-title-001") == "module-title"
    assert slugify("sample-9") == "sample"
    # Do not remove embedded numbers
    assert slugify("covid19-update") == "covid19-update"


def test_merge_equal_parts():
    assert merge_slug_parts("antibiotic-prophylaxis", "antibiotic-prophylaxis") == (
        "antibiotic-prophylaxis"
    )


def test_merge_suffix_overlap():
    # extra is suffix of base
    assert merge_slug_parts("antibiotic-prophylaxis", "prophylaxis") == (
        "antibiotic-prophylaxis"
    )


def test_merge_prefix_overlap():
    # base is prefix of extra
    assert merge_slug_parts("abc", "abc-def") == "abc-def"


def test_merge_token_overlap():
    # Overlap on tokens should not repeat
    assert merge_slug_parts("abc-def", "def-ghi") == "abc-def-ghi"


def test_merge_no_overlap():
    assert merge_slug_parts("abc", "ghi") == "abc-ghi"
