import re
import unicodedata


def slugify(value: str) -> str:
    """Return a URL-friendly slug.

    - Normalize unicode to NFKD and strip accents
    - Lowercase
    - Replace any non-alphanumeric (a-z, 0-9) with '-'
    - Collapse multiple '-' into one
    - Trim leading/trailing '-'
    """
    if value is None:
        return ""
    # Normalize and strip accents
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    # Lowercase
    value = value.lower()
    # Replace non-alphanumeric with '-'
    value = re.sub(r"[^a-z0-9]+", "-", value)
    # Collapse repeats and trim
    value = re.sub(r"-+", "-", value).strip("-")
    # Remove trailing numeric block (e.g., '-001') - REMOVED to keep years/numbered items
    # value = re.sub(r"-\d+$", "", value)

    return value


def sanitize_slug_component(value: str | None, fallback: str = "unknown") -> str:
    """Return a slug-safe component without consecutive hyphens.
    
    LME-compatible implementation matching app/utils/slug.py.
    """
    if not value:
        return fallback

    # Normalize and strip accents
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    lower_value = ascii_value.lower()
    # Replace any non-alphanumeric characters with hyphen
    cleaned = re.sub(r"[^a-z0-9]+", "-", lower_value)
    # Collapse multiple hyphens into one
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or fallback


def build_slug(prefix: str, *components: str | None, fallback: str = "unknown") -> str:
    """Construct a slug by sanitizing each component and joining with hyphens.
    
    LME-compatible implementation matching app/utils/slug.py.
    
    Args:
        prefix: The slug prefix (e.g., 'res', 'klp', 'mod')
        *components: Variable number of components to include in slug
        fallback: Fallback value for empty components
        
    Returns:
        A sanitized slug string like 'res-video-post-partum-hemorrhage'
    """
    if not prefix:
        raise ValueError("Slug prefix must be provided")

    sanitized_prefix = sanitize_slug_component(prefix, fallback)
    sanitized_components = [sanitize_slug_component(c, fallback) for c in components]
    parts = [sanitized_prefix, *sanitized_components]
    return "-".join(parts)


def merge_slug_parts(base: str, extra: str) -> str:
    """Merge two slug parts avoiding repetition.

    Rules:
    - If either is empty, return the other
    - If both equal, return one
    - If one is a prefix/suffix of the other, return the longer one
    - If there's a token overlap between the end of base and start of extra,
      merge without duplicating the overlap
    - Else join with '-'
    """
    base = base or ""
    extra = extra or ""
    if not base:
        return extra
    if not extra:
        return base
    if base == extra:
        return base
    # If one contains the other as prefix/suffix, prefer the longer
    if base.endswith(f"-{extra}") or base == extra or base.startswith(f"{extra}-"):
        return base
    if extra.endswith(f"-{base}") or extra.startswith(f"{base}-"):
        return extra

    # Try to merge by overlapping tokens
    # e.g., 'abc-def' + 'def-ghi' -> 'abc-def-ghi'
    base_tokens = base.split("-")
    extra_tokens = extra.split("-")

    # Find the largest k where suffix of base matches prefix of extra
    max_k = min(len(base_tokens), len(extra_tokens))
    overlap = 0
    for k in range(max_k, 0, -1):
        if base_tokens[-k:] == extra_tokens[:k]:
            overlap = k
            break

    if overlap > 0:
        merged_tokens = base_tokens + extra_tokens[overlap:]
        return "-".join(merged_tokens)

    return f"{base}-{extra}"