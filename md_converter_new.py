import os
import re
import html as html_module
import urllib.parse
from html.parser import HTMLParser
from typing import Dict, List, Optional
from configs import _get_assets_base_url, POST_ASSET
# DataFactory is imported inside functions to avoid circular import


# ---------------------------------------------------------------------------
# In-house HTML → Markdown converter
# ---------------------------------------------------------------------------

class _HTMLToMarkdownParser(HTMLParser):
    """Lightweight, stateful HTML-to-Markdown converter using stdlib html.parser.

    Supported tags:
        Block  : h1-h6, p, ul, ol, li, blockquote, hr, br
        Inline : strong, b, em, i, code, span
        Media  : img
        Table  : table, thead, tbody, tr, th, td
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []          # finished top-level markdown blocks
        self._inline_buf: str = ""           # text buffer for the current inline context
        self._list_stack: List[str] = []     # 'ul' or 'ol' with per-level item count
        self._list_counters: List[int] = []  # item counters for ol levels
        self._in_bold: bool = False
        self._in_italic: bool = False
        self._in_code: bool = False
        self._in_blockquote: bool = False
        self._heading_level: int = 0         # 0 = not in heading
        # Table state
        self._in_table: bool = False
        self._table_rows: List[List[str]] = []
        self._current_row: List[str] = []
        self._current_cell: str = ""
        self._cell_is_header: bool = False
        self._current_row_has_header: bool = False  # whether current row has any <th> cells
        self._header_row_index: int = -1     # which row index is the header row
        self._skip_tags = {"div", "span", "tbody", "thead", "tfoot",
                           "colgroup", "col", "caption", "section",
                           "article", "nav", "header", "footer",
                           "main", "aside", "figure", "figcaption"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _flush_inline(self) -> str:
        """Return current inline buffer and reset it."""
        text = self._inline_buf.strip()
        self._inline_buf = ""
        return text

    def _push_block(self, text: str):
        """Append a non-empty stripped block to _parts."""
        text = text.strip()
        if text:
            self._parts.append(text)

    def _list_indent(self) -> str:
        depth = len(self._list_stack)
        return "  " * (depth - 1) if depth > 0 else ""

    # ------------------------------------------------------------------
    # Tag handlers
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs):
        attrs_dict = dict(attrs)
        tag = tag.lower()

        # ------- Headings -------
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_level = int(tag[1])

        # ------- Paragraphs / HR / BR -------
        elif tag == "p":
            pass  # just accumulate inline content
        elif tag == "hr":
            self._push_block("---")
        elif tag == "br":
            self._inline_buf += "  \n"  # markdown line break

        # ------- Lists -------
        elif tag == "ul":
            self._list_stack.append("ul")
            self._list_counters.append(0)
        elif tag == "ol":
            self._list_stack.append("ol")
            self._list_counters.append(0)
        elif tag == "li":
            # flush any orphan text before starting an item
            orphan = self._flush_inline()
            if orphan:
                self._push_block(orphan)

        # ------- Blockquote -------
        elif tag == "blockquote":
            self._in_blockquote = True

        # ------- Inline emphasis -------
        elif tag in ("strong", "b"):
            self._in_bold = True
        elif tag in ("em", "i"):
            self._in_italic = True
        elif tag == "code":
            self._in_code = True

        # ------- Images -------
        elif tag == "img":
            src = attrs_dict.get("src", "")
            alt = attrs_dict.get("alt", "")
            # Flush any pending inline content before emitting the image,
            # then push the image as its own standalone block so it doesn't
            # bleed into the next paragraph.
            if src:
                orphan = self._flush_inline()
                if orphan:
                    self._push_block(orphan)
                self._push_block(f"![{alt}]({src})")

        # ------- Tables -------
        elif tag == "table":
            self._in_table = True
            self._table_rows = []
            self._current_row = []
            self._current_cell = ""
            self._header_row_index = -1
        elif tag == "tr":
            self._current_row = []
            self._cell_is_header = False
            self._current_row_has_header = False
        elif tag in ("th", "td"):
            self._current_cell = ""
            if tag == "th":
                self._cell_is_header = True

        # Silently skip wrapper/semantic tags
        # (handled via _skip_tags in end-tag or by doing nothing here)

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        # ------- Headings -------
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = self._heading_level
            text = self._flush_inline()
            if text:
                self._push_block("#" * level + " " + text)
            self._heading_level = 0

        # ------- Paragraphs -------
        elif tag == "p":
            text = self._flush_inline()
            if text:
                self._push_block(text)

        # ------- Lists -------
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
                self._list_counters.pop()
        elif tag == "li":
            indent = self._list_indent()
            text = self._flush_inline()
            if text:
                if self._list_stack and self._list_stack[-1] == "ol":
                    self._list_counters[-1] += 1
                    marker = f"{self._list_counters[-1]}."
                else:
                    marker = "-"
                self._push_block(f"{indent}{marker} {text}")

        # ------- Blockquote -------
        elif tag == "blockquote":
            self._in_blockquote = False

        # ------- Inline emphasis -------
        elif tag in ("strong", "b"):
            self._in_bold = False
        elif tag in ("em", "i"):
            self._in_italic = False
        elif tag == "code":
            self._in_code = False

        # ------- Tables -------
        elif tag in ("th", "td"):
            cell_text = self._current_cell.strip()
            if self._cell_is_header:
                self._current_row_has_header = True
            self._current_row.append(cell_text)
            self._current_cell = ""
            self._cell_is_header = False
        elif tag == "tr":
            if self._current_row:
                # Detect header row via the flag set when <th> cells were found
                if self._current_row_has_header and self._header_row_index == -1:
                    self._header_row_index = len(self._table_rows)
                self._table_rows.append(self._current_row)
                self._current_row = []
        elif tag == "table":
            md_table = self._render_table()
            if md_table:
                self._push_block(md_table)
            self._in_table = False
            self._table_rows = []

    def handle_data(self, data: str):
        # Normalise whitespace but preserve intentional newlines (\n inside cells)
        text = re.sub(r"[ \t\r]+", " ", data)  # collapse spaces/tabs
        text = html_module.unescape(text)

        if self._in_table:
            # Inside a table, accumulate into the current cell buffer
            self._current_cell += text
            return

        # Apply inline styles
        if self._in_code:
            text = f"`{text}`"
            
        # Apply standard inline styles
        if self._in_bold:
            stripped = text.strip()
            if stripped:
                text = text.replace(stripped, f"**{stripped}**")
        if self._in_italic:
            stripped = text.strip()
            if stripped:
                text = text.replace(stripped, f"*{stripped}*")
                    
        # Replace blockquote grey bar styling with specific #CF0048 bold pink text
        if self._in_blockquote:
            stripped = text.strip()
            # If the text was already made bold above, we don't need to double-bold it, 
            # but usually it isn't. To be safe, we just wrap whatever text in the pink color.
            # And we add bold if it wasn't already wrapped in **.
            if stripped and not stripped.startswith("**"):
                text = text.replace(stripped, f'<font color="#CF0048">**{stripped}**</font>')
            elif stripped:
                text = text.replace(stripped, f'<font color="#CF0048">{stripped}</font>')

        self._inline_buf += text

    # ------------------------------------------------------------------
    # Table rendering
    # ------------------------------------------------------------------

    def _render_table(self) -> str:
        if not self._table_rows:
            return ""

        # Determine column count
        col_count = max(len(row) for row in self._table_rows)

        # Pad all rows to same width
        padded = [row + [""] * (col_count - len(row)) for row in self._table_rows]

        # Compute column widths
        col_widths = [0] * col_count
        for row in padded:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(cell))

        def fmt_row(row: List[str]) -> str:
            cells = [cell.ljust(col_widths[i]) for i, cell in enumerate(row)]
            return "| " + " | ".join(cells) + " |"

        def separator() -> str:
            dashes = ["-" * max(col_widths[i], 3) for i in range(col_count)]
            return "| " + " | ".join(dashes) + " |"

        lines = []
        # If the table has a detected header row at index 0, emit separator after it
        header_idx = self._header_row_index if self._header_row_index >= 0 else None

        for i, row in enumerate(padded):
            lines.append(fmt_row(row))
            # Emit separator after the header row (or after first row if no explicit header)
            if (header_idx is not None and i == header_idx) or \
               (header_idx is None and i == 0):
                lines.append(separator())

        return "[TableStart]\n" + "\n".join(lines) + "\n[TableEnd]"

    # ------------------------------------------------------------------
    # Final output
    # ------------------------------------------------------------------

    def get_markdown(self) -> str:
        # Flush any remaining inline content
        leftover = self._flush_inline()
        if leftover:
            self._parts.append(leftover)
        return "\n\n".join(self._parts)


def convert_html_to_markdown(html_content: str) -> str:
    """Convert an HTML string to Markdown.

    Uses the in-house _HTMLToMarkdownParser (no third-party dependencies).
    Returns the original string unchanged if it does not look like HTML.
    """
    if not html_content or not isinstance(html_content, str):
        return html_content or ""
    stripped = html_content.strip()
    # Quick heuristic: must start with '<' to be treated as HTML
    if not stripped.startswith("<"):
        return html_content
    parser = _HTMLToMarkdownParser()
    parser.feed(stripped)
    return parser.get_markdown()


def extract_filename_from_path(path: str) -> str:
    decoded_path = urllib.parse.unquote(path)
    return os.path.basename(decoded_path)

_COLOR_STYLES = {
    "BLUE", "RED", "GREEN", "ORANGE", "YELLOW", "PURPLE", "PINK", "BROWN",
    "GREY", "GRAY",
}


def apply_styles(text: str, style_ranges: list) -> str:
    if not style_ranges:
        return text

    # Separate colour ranges from non-colour ranges so we can apply them
    # in two passes – both from right to left to keep earlier offsets valid.
    # COLOR is applied first (wrapping at original offsets), then BOLD is
    # applied second so that ** markers land inside the <color> tags,
    # producing correct nesting: <color style="red">**text**</color>.
    bold_ranges = []
    color_ranges = []
    for r in style_ranges:
        style = r.get("style", "")
        if style == "BOLD":
            bold_ranges.append(r)
        elif style in _COLOR_STYLES:
            color_ranges.append(r)

    # --- Pass 1: COLOR (right-to-left, at original offsets) ---
    color_ranges.sort(
        key=lambda r: r.get("offset", 0) + r.get("length", 0), reverse=True
    )
    for r in color_ranges:
        offset = r.get("offset", 0)
        length = r.get("length", 0)
        style = r.get("style", "")
        color_name = style.lower()
        text_len = len(text)
        if offset < 0 or offset > text_len:
            continue
        end = min(offset + length, text_len)
        text = (
            text[:offset]
            + f'<color style="#b5093f">**'
            + text[offset:end]
            + '**</color>'
            + text[end:]
        )

    # --- Pass 2: BOLD (right-to-left, after color tags are inserted) ---
    # Recalculate offsets: for each bold range, count how many color-tag
    # characters were inserted before it and shift accordingly.
    # We need to adjust bold offsets because color tags were inserted.
    _color_insertions = []
    for r in style_ranges:
        if r.get("style", "") in _COLOR_STYLES:
            ofs = r.get("offset", 0)
            ln = r.get("length", 0)
            open_tag_len = len('<color style="#b5093f">**')
            close_tag_len = len("**</color>")
            _color_insertions.append((ofs, open_tag_len, ofs + ln, close_tag_len))

    bold_ranges.sort(
        key=lambda r: r.get("offset", 0) + r.get("length", 0), reverse=True
    )
    for r in bold_ranges:
        offset = r.get("offset", 0)
        length = r.get("length", 0)
        # Adjust offset and end for any color tags that were inserted
        adj_offset = offset
        adj_end = offset + length
        for c_start, c_open_len, c_end, c_close_len in _color_insertions:
            if c_start <= offset:
                adj_offset += c_open_len
                adj_end += c_open_len
            if c_end < offset + length:
                adj_end += c_close_len
        text_len = len(text)
        if adj_offset < 0 or adj_offset > text_len:
            continue
        adj_end = min(adj_end, text_len)
        text = (
            text[:adj_offset]
            + "**"
            + text[adj_offset:adj_end].strip()
            + "**"
            + text[adj_end:]
        )

    return text


def parse_rich_text_block(content_block: dict) -> str:
    if not content_block or "blocks" not in content_block:
        return ""
    lines = []
    for block in content_block["blocks"]:
        text = block.get("text", "")
        styles = block.get("inlineStyleRanges", [])
        block_type = block.get("type", "")
        
        # Strip BOLD and ITALIC if the block is a blockquote, because the
        # frontend parser does not support nested markdown inside blockquotes
        if block_type == "blockquote":
            styles = [s for s in styles if s.get("style") not in ("BOLD", "ITALIC")]
            
        styled = apply_styles(text, styles)
        
        if block_type == "blockquote":
            styled = f"> {styled}"
            
        lines.append(styled)
    return "\n".join(lines).strip()


def process_embedded_images(markdown: str, language_id: str = "global") -> str:
    """Find embedded image paths in markdown and replace with Asset IDs.
    
    Converts: ![alt](relative/path.png) -> ![alt](asset-uuid)
    Skips: Already processed (http URLs, UUIDs)
    """
    from factories import DataFactory
    
    # Pattern for markdown images: ![alt](path)
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    
    def replace_image(match):
        alt_text = match.group(1)
        image_path = match.group(2)
        
        # Skip if already a URL or UUID (already processed)
        if image_path.startswith("http") or len(image_path) == 36:  # UUID length
            return match.group(0)
        
        # Skip if looks like an asset ID (contains only hex and dashes)
        if re.match(r'^[a-f0-9-]+$', image_path.lower()):
            return match.group(0)
        
        print(f"    📷 Processing embedded image: {image_path}")
        
        # Construct full URL for the image
        clean_path = image_path.lstrip("/")
        base_url = _get_assets_base_url().rstrip("/")
        
        # Add .png extension if missing
        if not clean_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
            clean_path = f"{clean_path}.png"
        
        full_url = f"{base_url}/images/{clean_path}"
        local_path = f"media_storage/{extract_filename_from_path(full_url)}"
        
        # Upload and get asset ID
        upload_url = POST_ASSET.format(tag="image")
        result = DataFactory.get_or_upload_asset_with_local_cache(
            source_url=full_url,
            local_path=local_path,
            media_type="image/png",
            upload_url=upload_url
        )
        
        asset_id = result.get("asset_id") if result else None
        
        if asset_id:
            print(f"    ✅ Replaced with Asset ID: {asset_id}")
            return f"![{alt_text}]({asset_id})"
        else:
            print(f"    ⚠️ Failed to upload, keeping original path")
            return match.group(0)
    
    try:
        return re.sub(pattern, replace_image, markdown)
    except Exception as e:
        from error_logger import log_error
        log_error("Captured Exception", exc=e)
        print(f"Error processing embedded images: {e}")
        return markdown


def process_card(card: dict, version_key: str, asset_version: str, strict_fallback: bool = False) -> dict:
    from factories import DataFactory  # Avoid circular import

    card_type = card.get("type")
    content = card.get(version_key)
    # Check semantic emptiness: a dict with empty/blank blocks is useless
    if isinstance(content, dict):
        blocks = content.get("blocks", [])
        has_text = any(b.get("text", "").strip() for b in blocks if isinstance(b, dict))
        if not has_text and not content.get("html") and not content.get("src"):
            content = None
    # Fallback chain: translated → adapted → content (English)
    if not content and version_key == "translated":
        content = card.get("adapted")
        # Re-check semantic emptiness for adapted fallback
        if isinstance(content, dict):
            blocks = content.get("blocks", [])
            has_text = any(b.get("text", "").strip() for b in blocks if isinstance(b, dict))
            if not has_text and not content.get("html") and not content.get("src"):
                content = None
    # English original as absolute last resort — but ONLY for non-text cards
    # (images, dividers, HTML tables). Text cards must never fall back to
    # English; missing translated text is preferable to wrong-language text.
    if not content:
        if card_type in ("image", "divider", "divider_noline"):
            content = card.get("content")
        elif not strict_fallback and version_key != "translated":
            # For original/adapted pipelines, English fallback is fine
            content = card.get("content")
        else:
            # Check if content has HTML (tables are structural, not language-specific)
            eng_content = card.get("content")
            if isinstance(eng_content, dict) and "html" in eng_content:
                content = eng_content
            elif isinstance(eng_content, dict) and eng_content.get("src"):
                content = eng_content  # Image src in dict form

    if not content:
        return {"md_text": "", "asset_id": None}

    # Handle dict with 'html' key (e.g. table cards)
    if isinstance(content, dict) and "html" in content:
        html_str = content["html"]
        if isinstance(html_str, str) and html_str.strip():
            md_text = convert_html_to_markdown(html_str)
            return {"md_text": md_text, "asset_id": None}

    # -----------------------------------------------------------------
    # If content is a raw HTML string, convert it in-house to Markdown.
    # This handles cards whose content comes directly as an HTML string
    # (e.g. containing <table>, <ul>, <h3> etc.) rather than as a
    # Draft.js rich-text block dict.
    # -----------------------------------------------------------------
    if isinstance(content, str) and content.strip().startswith("<"):
        md_text = convert_html_to_markdown(content)
        return {"md_text": md_text, "asset_id": None}

    md_text = parse_rich_text_block(content)

    # Strip redundant bold from heading-type cards — the ### / #### prefix
    # already implies emphasis; wrapping in ** creates double-bold markers.
    if card_type in ("header", "subheader", "alphabetical") and md_text:
        md_text = re.sub(r'^\*\*(.+?)\*\*$', r'\1', md_text.strip())

    if not md_text and card_type in ("divider", "divider_noline", "image"):
        if card_type in ("divider", "divider_noline"):
            return {"md_text": "", "asset_id": None}
        if card_type == "image":
            raw_src = content.get('src', '').strip()
            # Remove leading slash to ensure clean joining
            if raw_src.startswith("/"):
                raw_src = raw_src[1:]
            
            # Construct path parts filtering out empty values
            path_parts = [p for p in [asset_version, raw_src] if p]
            joined_path = "/".join(path_parts)
            
            base_url = _get_assets_base_url().rstrip("/")
            img_src = f"{base_url}/images/{joined_path}.png"
            file_path = f"media_storage/{extract_filename_from_path(img_src)}"

            # Use DataFactory for caching/uploading
            upload_url = POST_ASSET.format(tag="image")
            image_upload = DataFactory.get_or_upload_asset_with_local_cache(
                source_url=img_src,
                local_path=file_path,
                media_type="image/png",
                upload_url=upload_url
            )

            asset_id = image_upload.get("asset_id") if image_upload else None
            return {
                "md_text": f"![Image]({asset_id})" if asset_id else "",
                "asset_id": asset_id,
            }

    prefix = ""
    if card_type == "alphabetical":
        prefix = "### "
    elif card_type == "important_text":
        prefix = "> "
    elif card_type == "header":
        prefix = "### "
    elif card_type == "subheader":
        prefix = "## "
    elif card_type == "ul":
        lines = [line for line in md_text.splitlines() if line.strip()]
        md_text = "\n".join([f"- {line}" for line in lines])
        prefix = ""
    elif card_type == "ol":
        lines = [line for line in md_text.splitlines() if line.strip()]
        md_text = "\n".join([f"{i+1}. {line}" for i, line in enumerate(lines)])
        prefix = ""

    # Don't return bare prefix markers (e.g. "##") when card text is empty
    if not md_text and prefix:
        return {"md_text": "", "asset_id": None}

    return {"md_text": f"{prefix}{md_text}".strip(), "asset_id": None}



def save_markdown_file(path: str, parts: list, language_id: str = "global"):
    """Save markdown file, processing any embedded images to replace paths with Asset IDs."""
    final = "\n\n".join(filter(None, parts))
    
    # Process embedded images: replace relative paths with Asset IDs
    final = process_embedded_images(final, language_id)
    
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(final)
        print(f"Saved Markdown: {path}")
    except Exception as e:
        from error_logger import log_error
        log_error("Captured Exception", exc=e)
        print(f"Failed to save Markdown: {e}")


def convert_about_to_md_versions(about_doc):
    """Convert about document to markdown format for all versions"""
    versions = {"content": [], "adapted": [], "translated": []}
    
    for chapter in about_doc.get("chapters", []):
        for card in chapter.get("cards", []):
            # Process each version
            for version in versions.keys():
                version_data = card.get(version, {})
                blocks = version_data.get("blocks", []) if version_data else []
                
                # Handle ordered lists properly
                if card.get("type") == "ol" or any(block.get("type") == "ordered-list-item" for block in blocks):
                    list_items = []
                    for i, block in enumerate(blocks, 1):
                        text = block.get("text", "").strip()
                        if text:
                            list_items.append(f"{i}. {text}")
                    if list_items:
                        versions[version].append("\n".join(list_items))
                else:
                    for block in blocks:
                        text = block.get("text", "").strip()
                        if not text:
                            continue
                        block_type = block.get("type", "unstyled")
                        card_type = card.get("type", "paragraph")
                        
                        if card_type == "header" or block_type == "header":
                            versions[version].append(f"# {text}")
                        elif card_type == "subheader" or block_type == "subheader":
                            versions[version].append(f"## {text}")
                        elif block_type == "unordered-list-item":
                            versions[version].append(f"- {text}")
                        else:
                            versions[version].append(text)
    
    return {
        "content": "\n\n".join(versions["content"]),
        "adapted": "\n\n".join(versions["adapted"]) if versions["adapted"] else "\n\n".join(versions["content"]),
        "translated": "\n\n".join(versions["translated"]) if versions["translated"] else "\n\n".join(versions["content"])
    }



def convert_action_card_to_markdown_files(
    action_card_doc: dict,
    output_dir: str = "temp_data",
    *,
    region: Optional[str] = None,
    country_code: Optional[str] = None,
    language_name: Optional[str] = None,
    allowed_versions: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Convert action-card to markdown with HEADER AS FIRST LINE."""
    import unicodedata
    from text_utils import clean_and_resolve_title, format_mobile_markdown

    title = clean_and_resolve_title(
        action_card_doc.get("title") or action_card_doc.get("description", ""), 
        action_card_doc.get("key", "")
    ) or "Untitled Action Card"
    
    # Look for translated title from chapters > cards > translated header
    translated_title = None
    chapters_for_title = action_card_doc.get("chapters", [])
    if not chapters_for_title and "cards" in action_card_doc:
        chapters_for_title = [{"cards": action_card_doc["cards"]}]
    for chap in chapters_for_title:
        cards_for_header = chap.get("cards", [])
        if not cards_for_header and ("translated" in chap or "content" in chap):
            cards_for_header = [chap]
            
        for card in cards_for_header:
            if card.get("type") in ("header", "alphabetical"):
                trans_content = card.get("translated") or {}
                if isinstance(trans_content, dict):
                    header_text = parse_rich_text_block(trans_content)
                    if header_text and header_text.strip():
                        translated_title = header_text.strip()
                        break
        if translated_title:
            break

    def _slugify_filename_base(text: str) -> str:
        norm = unicodedata.normalize("NFKD", text or "")
        ascii_text = norm.encode("ascii", "ignore").decode("ascii")
        ascii_text = ascii_text.lower()
        ascii_text = re.sub(r"[^a-z0-9]+", "_", ascii_text)
        ascii_text = re.sub(r"_+", "_", ascii_text).strip("_")
        return ascii_text if ascii_text else "action_card"

    safe_title = _slugify_filename_base(title)
    region_slug = _slugify_filename_base(region or "")
    language_slug = _slugify_filename_base(language_name or "")

    chapters = action_card_doc.get("chapters", [])
    if not chapters and "cards" in action_card_doc:
        chapters = [{"cards": action_card_doc["cards"]}]

    # For translated: use translated_title if found, otherwise skip header
    # (the first header card will naturally be processed and included)
    has_chapters_init = bool(chapters)
    versions: Dict[str, List[str]] = {
        "original": [f"# {title}"] if (title and not has_chapters_init) else [],
        "adapted": [f"# {title}"] if (title and not has_chapters_init) else [],
        "translated": ([f"# {translated_title}"] if (translated_title and not has_chapters_init) else []),
    }

    asset_version = region or ""

    for chapter in chapters:
        cards = chapter.get("cards", [])
        if not cards and ("content" in chapter or "adapted" in chapter or "translated" in chapter):
            cards = [chapter]

        # ── Chapter heading resolution ──
        # PRIORITY: Use translated chapter headings from the Cosmos DB `screens`
        # table (injected into chapter dict by factories.py as
        # screen_translated_title / screen_adapted_title / screen_content_title).
        # FALLBACK: Extract from the first header card's Draft.js blocks.
        # LAST RESORT: chapter.description (English).
        #
        # When the first card is a heading-type card (header/subheader/alphabetical)
        # and it was used as the chapter title source OR screens data is present,
        # we skip it from the body loop to avoid rendering the title twice.

        version_key_map = {
            "original": "content",
            "adapted": "adapted",
            "translated": "translated",
        }

        # Determine if the first card is a heading-type card
        first_card_is_heading = (
            cards
            and cards[0].get("type") in ("header", "subheader", "alphabetical")
        )
        # Track whether we successfully obtained a chapter title (any version)
        got_chapter_title = False

        for v_name, vk in version_key_map.items():
            # 1. Screens table data (best source for localised titles)
            screen_key_map = {
                "original": "screen_content_title",
                "adapted": "screen_adapted_title",
                "translated": "screen_translated_title",
            }
            chapter_title = chapter.get(screen_key_map[v_name])

            # Fallback for translated: adapted → content
            if not chapter_title and v_name == "translated":
                chapter_title = chapter.get("screen_adapted_title")
            if not chapter_title and v_name in ("translated", "adapted"):
                chapter_title = chapter.get("screen_content_title")

            # 2. First header card fallback (used when screens data absent)
            if not chapter_title and first_card_is_heading:
                first_card = cards[0]
                header_block = first_card.get(vk)
                # Semantic check: skip empty translated dicts
                if isinstance(header_block, dict):
                    hb_blocks = header_block.get("blocks", [])
                    hb_has_text = any(b.get("text", "").strip() for b in hb_blocks if isinstance(b, dict))
                    if not hb_has_text:
                        header_block = None
                # Translated fallback: adapted → content
                if not header_block and vk == "translated":
                    header_block = first_card.get("adapted")
                    if isinstance(header_block, dict):
                        hb_blocks = header_block.get("blocks", [])
                        if not any(b.get("text", "").strip() for b in hb_blocks if isinstance(b, dict)):
                            header_block = None
                if not header_block:
                    header_block = first_card.get("content")
                header_text = parse_rich_text_block(header_block) if isinstance(header_block, dict) else ""
                if header_text and header_text.strip():
                    chapter_title = header_text.strip()

            # 3. Last resort: English description
            if not chapter_title:
                chapter_title = chapter.get("description", "")

            if chapter_title:
                versions[v_name].append(f"# Chapter: {chapter_title}")

        # Process ALL cards — the chapter heading is an external metadata
        # wrapper sourced from screens/description; first card is ALWAYS
        # a body card (rendered as ### by process_card) and must not be skipped.
        for card in cards:
            # Process each version
            content_card = process_card(card, "content", asset_version)
            if content_card["md_text"]:
                versions["original"].append(content_card["md_text"])

            # Get genuine adapted text (no English fallback) so translated can safely fallback to it
            raw_adapted_card = process_card(card, "adapted", asset_version, strict_fallback=True)
            
            adapted_card = process_card(card, "adapted", asset_version)
            if not adapted_card["md_text"]:
                adapted_card = content_card
            if adapted_card["md_text"]:
                versions["adapted"].append(adapted_card["md_text"])

            translated_card = process_card(card, "translated", asset_version, strict_fallback=True)
            if not translated_card["md_text"]:
                # Fallback to REAL adapted only — never English content_card.
                # raw_adapted_card is the result before adapted's own English fallback.
                if raw_adapted_card["md_text"]:
                    translated_card = raw_adapted_card
            if translated_card["md_text"]:
                versions["translated"].append(translated_card["md_text"])

        for v in versions.values():
            v.append("---")

    out_paths: Dict[str, str] = {}
    allowed_set = set(allowed_versions) if allowed_versions else None

    for version_name, parts in versions.items():
        if allowed_set and version_name not in allowed_set:
            continue
        if not parts:
            continue
        
        translated_suffix = language_slug or "unknown"
        suffix_bits = {
            "original": ["original"],
            "adapted": ["adapted", region_slug] if region_slug else ["adapted"],
            "translated": ["translated", translated_suffix],
        }.get(version_name, [version_name])
        suffix = "_".join([bit for bit in suffix_bits if bit])
        
        path = os.path.join(output_dir, f"{safe_title}_{suffix}.md")
        # Join with double newlines (one blank line between card blocks)
        final_text = "\n\n".join(parts)

        # ── List compaction ─────────────────────────────────────────────
        # DraftJS stores every step as a separate unstyled card, so the
        # card loop injects \n\n between them.  When consecutive cards are
        # numbered/lettered/bulleted plain-text items (e.g. "1.\tStep"),
        # those double newlines cause Markdown parsers to split them into
        # independent <p> elements rather than a single <ol>/<ul>.
        # This pass collapses the gap between adjacent list-like lines so
        # the renderer sees one continuous block.
        #
        # Pattern matches a line that starts with a list marker FOLLOWED
        # immediately by \n\n and THEN another list marker line.
        # We replace \n\n with \n (single newline) to merge them.
        _LIST_MARKER = r"[ \t]*(?:\d+[.)]|[a-zA-Z][.)\]]|[*\-•])[\t ]+"
        final_text = re.sub(
            r"(" + _LIST_MARKER + r"[^\n]+)\n\n(?=" + _LIST_MARKER + r")",
            r"\1\n",
            final_text,
            flags=re.MULTILINE,
        )
        # ────────────────────────────────────────────────────────────────

        # Determine effective title
        effective_title = title
        if version_name == "translated" and translated_title:
            effective_title = translated_title

        # Bug 2.4 fix: Skip format_mobile_markdown for multi-chapter docs
        # — they already have '# Chapter:' structure and wrapping would
        # insert a redundant outer title header.
        has_chapters = bool(chapters and len(chapters) > 0)
        if has_chapters:
            formatted_text = final_text
        else:
            formatted_text = format_mobile_markdown(effective_title, final_text)

        save_markdown_file(path, [formatted_text])
        out_paths[version_name] = path

    return out_paths
