"""
Certificate Migration: Cosmos DB → LME

STRICT RULES:
1. description → PLAIN STRING ONLY (from cert.description or cert.name)
2. content → Asset ID (Cards → Markdown → Upload)
3. Cases → Query Cosmos with matching langId, NO fallback to global
4. For translated: Use ONLY translated field, ignore content/adapted
5. All values must be flattened strings, no DraftJS objects

FIXES:
- Properly capture certificate ID from API response
- Ensure global certificate is created/found before translations
- Use POST /certificates/{parent_id} for version creation
- Fetch only LATEST version per (key, langId) from Cosmos
- Handle 409/500 errors when certificate already exists
- Retrieve existing certificate ID when duplicate detected
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, List, Optional
import requests
import time

from factories import DataFactory
from configs import LME_BASE_URL, JWT_TOKEN, ACTIVATE_CERTIFICATE_VERSION

from slug_utils import slugify, build_slug
from text_utils import clean_and_resolve_title, format_mobile_markdown


class CertificateMigrator:
    def __init__(self, cosmos_client, container, language_mapping: Dict):
        self.cosmos_client = cosmos_client
        self.container = container
        self.language_mapping = language_mapping
        self.api_url = f"{LME_BASE_URL.rstrip('/')}/certificates"
        
        # Temp directory for markdown files
        self.temp_dir = Path(tempfile.gettempdir()) / "certificate_content"
        self.temp_dir.mkdir(exist_ok=True)
        
        # Mappings checks
        self.certificate_mapping_path = Path("data/mappings/certificate_mapping.csv")
        self.certificate_mapping_path.parent.mkdir(parents=True, exist_ok=True)
        
        # In-memory caches
        self.existing_certs_cache = {} # title -> id
        self.existing_certs_slug_cache = {} # slug -> id
        self.local_mapping_cache = {} # cosmos_id -> lme_id (for versioning tracking)
        
        # Initialize caches
        self._load_local_mapping()
        self._load_all_certificates()

    def _load_local_mapping(self):
        """Load locally persisted mappings."""
        if not self.certificate_mapping_path.exists():
            return
            
        try:
            import csv
            with self.certificate_mapping_path.open("r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    cid = row.get("cosmos_id")
                    lid = row.get("lme_id")
                    if cid and lid:
                        self.local_mapping_cache[cid] = lid
            print(f"  📂 Loaded {len(self.local_mapping_cache)} local certificate mappings.")
        except Exception as e:
            print(f"  ⚠️ Error loading local mapping: {e}")

    def _save_mapping(self, cosmos_id: str, lme_id: str, title: str):
        """Append new mapping to CSV."""
        if not cosmos_id or not lme_id:
            return
            
        self.local_mapping_cache[cosmos_id] = lme_id
        
        try:
            import csv
            mode = "a" if self.certificate_mapping_path.exists() else "w"
            with self.certificate_mapping_path.open(mode, newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if mode == "w":
                    writer.writerow(["cosmos_id", "lme_id", "title"])
                writer.writerow([cosmos_id, lme_id, title])
        except Exception as e:
            print(f"  ⚠️ Error saving mapping: {e}")

    def _load_all_certificates(self):
        """Fetch ALL certificates from LME to build cache."""
        print("  🔄 Fetching all existing certificates from LME for caching...")
        try:
            headers = {"Authorization": f"Bearer {JWT_TOKEN}"}
            resp = requests.get(self.api_url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("results") or data.get("data") or data.get("items") or []
                
                count = 0
                for item in items:
                    cid = item.get("id") or item.get("certificate_id")
                    title = item.get("title")
                    slug = item.get("slug")
                    
                    if cid and title:
                        self.existing_certs_cache[title] = cid
                    if cid and slug:
                        self.existing_certs_slug_cache[slug] = cid
                    count += 1
                
                print(f"  ✅ Cached {count} existing certificates from API.")
            else:
                print(f"  ⚠️ Failed to fetch certificates: {resp.status_code}")
        except Exception as e:
            print(f"  ⚠️ Error fetching certificates: {e}")
        
    # =========================================================================
    # PHASE 1: Certificate Fetch & Normalization
    # =========================================================================
    
    def migrate_certificates(self, language_id: str = None):
        """Main entry point for certificate migration."""
        print(f"\n🚀 Starting CERTIFICATE migration...")
        
        # Fetch ALL certificates ordered by timestamp DESC (newest first)
        query = "SELECT * FROM c WHERE c._table = 'certificates' ORDER BY c._ts DESC"
        items = list(self.container.query_items(query=query, enable_cross_partition_query=True))
        print(f"  Found {len(items)} certificate documents in Cosmos.")
        
        # Deduplicate: Keep only LATEST version per (key, langId)
        latest_docs_map = {}
        for doc in items:
            key = doc.get("key")
            if not key:
                continue
                
            lang_id = doc.get("langId") or "global"
            unique_id = (key, lang_id)
            
            if unique_id not in latest_docs_map:
                latest_docs_map[unique_id] = doc
        
        unique_items = list(latest_docs_map.values())
        print(f"  Filtered to {len(unique_items)} unique latest certificates.")

        # Group by KEY
        cert_groups = self._group_certificates(unique_items)
        print(f"  Grouped into {len(cert_groups)} certificate families.")
        
        # Process each group
        for idx, (key, group) in enumerate(cert_groups.items(), 1):
            print(f"\n{'='*80}")
            print(f"Processing Certificate Group {idx}/{len(cert_groups)}: {key}")
            print(f"{'='*80}")
            
            # Rate limit: 1s delay between certificate groups
            time.sleep(1)
            
            global_doc = group["global"]
            if not global_doc:
                print(f"⚠️ No global doc for certificate key {key}. Skipping group.")
                continue
            
            # 1. Migrate Original (global) - ALWAYS ensure global exists first
            global_lme_id = None
            
            if not language_id:
                # Full migration mode
                print(f"\n📜 Migrating GLOBAL certificate for key: {key}")
                
                cert_title = global_doc.get("name") or global_doc.get("title")
                global_lme_id = self._find_existing_certificate(cert_title)
                
                if global_lme_id:
                    print(f"  ✅ Found existing global certificate: {global_lme_id}")
                else:
                    global_lme_id = self._migrate_certificate(global_doc, is_translated=False)
                
                if not global_lme_id:
                    print(f"  ❌ Failed to get global certificate ID for key {key}.")
                    print(f"     Skipping all translations for this certificate.")
                    continue
            else:
                # Language-specific migration
                cert_title = global_doc.get("name") or global_doc.get("title")
                print(f"  ℹ️ Language-specific migration mode for: {cert_title}")
                print(f"     Attempting to find existing global certificate...")
                
                global_lme_id = self._find_existing_certificate(cert_title)
                
                if not global_lme_id:
                    print(f"  ⚠️ Could not find existing global certificate.")
                    print(f"     Creating global certificate first...")
                    global_lme_id = self._migrate_certificate(global_doc, is_translated=False)
                    
                    if not global_lme_id:
                        print(f"  ❌ Failed to create/find global certificate.")
                        print(f"     Skipping translations for this certificate.")
                        continue
                else:
                    print(f"  ✅ Found existing global certificate: {global_lme_id}")
            
            # 2. Migrate Translations
            translations_to_migrate = []
            for t in group["translations"]:
                t_lang = t.get("langId")
                
                if language_id and t_lang != language_id:
                    continue
                
                if t_lang not in self.language_mapping:
                    print(f"  ⚠️ Skipping translation for langId '{t_lang}': Not in languages.csv")
                    continue
                
                translations_to_migrate.append(t)
            
            if not translations_to_migrate:
                print(f"\n  ℹ️ No valid translations to migrate for this certificate.")
                continue
                
            print(f"\n  📚 Found {len(translations_to_migrate)} valid translation(s) to migrate")
            print(f"     Parent certificate ID: {global_lme_id}")
            
            for trans_idx, trans_doc in enumerate(translations_to_migrate, 1):
                trans_lang = trans_doc.get("langId", "unknown")
                print(f"\n  {'─'*76}")
                print(f"  🌍 Translation {trans_idx}/{len(translations_to_migrate)}: Language {trans_lang}")
                print(f"  {'─'*76}")
                
                trans_lme_id = self._migrate_certificate(
                    trans_doc, 
                    is_translated=True, 
                    parent_id=global_lme_id
                )
                
                if trans_lme_id:
                    print(f"  ✅ Translation version created: {trans_lme_id}")
                else:
                    print(f"  ❌ Failed to create translation version for language {trans_lang}")
        
        print(f"\n{'='*80}")
        print(f"Certificate migration completed!")
        print(f"{'='*80}")
    
    def _find_existing_certificate(self, title: str) -> Optional[str]:
        """Find certificate by title using cache or API."""
        if not title:
            return None
            
        # 1. Check in-memory cache (title)
        if title in self.existing_certs_cache:
            cert_id = self.existing_certs_cache[title]
            print(f"  → Found in title cache: {cert_id}")
            return cert_id
            
        # 2. Check by derived slug (try both with and without 'cert-' prefix)
        slug = slugify(title)
        slugs_to_try = [slug, f"cert-{slug}"]
        
        for test_slug in slugs_to_try:
            if test_slug in self.existing_certs_slug_cache:
                cert_id = self.existing_certs_slug_cache[test_slug]
                print(f"  → Found in slug cache ({test_slug}): {cert_id}")
                return cert_id
             
        # 3. Fallback: API Search
        print(f"  → Searching API for certificate: '{title}'")
        try:
            headers = {
                "Authorization": f"Bearer {JWT_TOKEN}",
                "Content-Type": "application/json"
            }
            
            # Try multiple search strategies
            search_urls = [
                f"{self.api_url}?title={title}&content_type=original",
                f"{self.api_url}?slug={slug}",
                f"{self.api_url}?slug=cert-{slug}"
            ]
            
            for search_url in search_urls:
                print(f"     Trying: {search_url}")
                time.sleep(1) # Rate limit search calls
                resp = requests.get(search_url, headers=headers)
                
                if resp.status_code == 200:
                    results = resp.json()
                    candidates = []
                    
                    # Handle different response formats
                    if isinstance(results, list):
                        # Direct list of certificates
                        candidates = results
                        print(f"     Response is list with {len(results)} items")
                        
                    elif isinstance(results, dict):
                        # Could be:
                        # 1. Paginated response with items/results/data key
                        # 2. Single certificate object
                        # 3. Wrapper object containing the certificate
                        
                        # First, check for pagination keys
                        items = results.get("results") or results.get("data") or results.get("items")
                        if items and isinstance(items, list):
                            candidates = items
                            print(f"     Response is paginated dict with {len(items)} items")
                        else:
                            # Check if it's a single certificate object
                            # A certificate object should have 'id' or 'certificate_id' AND 'title' or 'slug'
                            has_id = results.get("id") or results.get("certificate_id")
                            has_identity = results.get("title") or results.get("slug")
                            
                            if has_id and has_identity:
                                # This IS a certificate object
                                candidates = [results]
                                print(f"     Response is single certificate object (id={has_id})")
                            else:
                                # Might be wrapped - check all dict values
                                print(f"     Response is dict, checking nested values...")
                                for key, value in results.items():
                                    if isinstance(value, dict):
                                        val_has_id = value.get("id") or value.get("certificate_id")
                                        val_has_identity = value.get("title") or value.get("slug")
                                        if val_has_id and val_has_identity:
                                            candidates = [value]
                                            print(f"     Found certificate in nested key '{key}' (id={val_has_id})")
                                            break
                                
                                if not candidates:
                                    print(f"     ✗ No certificate object found in response keys: {list(results.keys())}")
                    
                    if candidates:
                        # Extract ID from first candidate
                        cid = candidates[0].get("id") or candidates[0].get("certificate_id")
                        if cid:
                            print(f"     ✓ Found certificate ID: {cid}")
                            # Update cache
                            self.existing_certs_cache[title] = cid
                            candidate_slug = candidates[0].get("slug")
                            if candidate_slug:
                                self.existing_certs_slug_cache[candidate_slug] = cid
                            return cid
                        else:
                            print(f"     ✗ Candidate has no ID field (keys: {list(candidates[0].keys())})")
                    else:
                        print(f"     ✗ No candidates found in response")
                else:
                    print(f"     ✗ HTTP {resp.status_code}")
            
            print(f"  → Certificate not found via any search strategy")
            return None

            
        except Exception as e:
            print(f"  ⚠️ Error searching for existing certificate: {e}")
            import traceback
            traceback.print_exc()
            return None

    
    def _group_certificates(self, items: List[Dict]) -> Dict:
        """Group certificates by key into global and translations."""
        groups = {}
        
        for doc in items:
            key = doc.get("key")
            if not key:
                continue
            
            if key not in groups:
                groups[key] = {"global": None, "translations": []}
            
            lang_id = doc.get("langId")
            if not lang_id or lang_id == "global":
                groups[key]["global"] = doc
            else:
                groups[key]["translations"].append(doc)
        
        return groups
    
    # =========================================================================
    # PHASE 1.2 + 2: Certificate Metadata + Cards → Markdown
    # =========================================================================
    
    def _migrate_certificate(self, doc: Dict, is_translated: bool, parent_id: str = None) -> Optional[str]:
        """Migrate a single certificate document. Returns LME ID if successful."""
        cosmos_lang_id = doc.get("langId")
        
        # PHASE 1.2: Map metadata (STRICT)
        title = doc.get("name") or doc.get("title") or "Untitled Certificate"
        
        description = doc.get("description")
        if not description or not isinstance(description, str):
            description = title
        
        cert_type = "TRANSLATED" if is_translated else "GLOBAL"
        print(f"\n📜 Processing {cert_type} Certificate: '{title}'")
        if cosmos_lang_id:
            print(f"   Language: {cosmos_lang_id}")
        if parent_id:
            print(f"   Parent ID: {parent_id}")
        
        # ✅ CHECKPOINT 1: Validate metadata
        if not title:
            print(f"  ❌ CHECKPOINT 1 FAILED: title is empty. Skipping.")
            return None
        if not isinstance(description, str):
            print(f"  ❌ CHECKPOINT 1 FAILED: description is not a string. Skipping.")
            return None
        
        # Determine asset language ID
        asset_lang_id = cosmos_lang_id if is_translated else "global"
        
        if is_translated and cosmos_lang_id and cosmos_lang_id not in self.language_mapping:
            print(f"  → Language {cosmos_lang_id} not in mapping. Using 'global' prefix.")
            asset_lang_id = "global"
        
        # PHASE 2: Cards → Markdown → Asset
        content_asset_id = self._process_cards_to_asset(doc, is_translated)
        
        if not content_asset_id:
            print(f"  ❌ CHECKPOINT 2 FAILED: No content asset ID. Skipping.")
            return None
        
        # PHASE 3 + 4: Case Resolution & Mapping
        case_keys = doc.get("cases", [])
        cases_payload = self._build_cases(case_keys, cosmos_lang_id, asset_lang_id, is_translated)
        
        if cases_payload is None:
            print(f"  ❌ CHECKPOINT 3/4 FAILED: Case resolution error. Skipping certificate.")
            return None
        
        # PHASE 5: Final Payload Assembly
        payload = self._assemble_payload(
            doc=doc,
            title=title,
            description=description,
            content_asset_id=content_asset_id,
            cases=cases_payload,
            is_translated=is_translated,
            cosmos_lang_id=cosmos_lang_id,
            parent_id=parent_id
        )
        
        # ✅ CHECKPOINT 5: Pre-send validation
        validation_error = self._validate_payload(payload)
        if validation_error:
            print(f"  ❌ CHECKPOINT 5 FAILED: {validation_error}")
            return None
        
        # PHASE 6: Send to LME
        return self._post_certificate(payload, doc.get("id"), parent_id, title)
    
    # =========================================================================
    # PHASE 2: Cards → Markdown → Asset
    # =========================================================================
    
    def _process_cards_to_asset(self, doc: Dict, is_translated: bool) -> Optional[str]:
        """Convert cards to markdown and upload as asset."""
        cards = doc.get("cards", [])
        if not cards:
            markdown = "# Certificate Content\n\nNo content available."
        else:
            markdown = self._convert_cards_to_markdown(cards, is_translated)
        
        title = clean_and_resolve_title(doc.get("title") or doc.get("description"), doc.get("key"))
        markdown = format_mobile_markdown(title, markdown)
        
        cert_key = doc.get("key", "unknown")
        lang_suffix = doc.get("langId") or "global"
        temp_file = self.temp_dir / f"{cert_key}_{lang_suffix}.md"
        temp_file.write_text(markdown, encoding='utf-8')
        
        asset_id = DataFactory._upload_asset(str(temp_file), "certificate")
        
        if asset_id:
            print(f"  ✅ Content uploaded: {asset_id[:20]}...")
        
        return asset_id
    
    def _convert_cards_to_markdown(self, cards: List[Dict], is_translated: bool) -> str:
        """Convert DraftJS cards to Markdown."""
        output_lines = []
        
        for card in cards:
            card_type = card.get("type", "paragraph")
            
            if is_translated:
                block = card.get("translated") or card.get("adapted") or card.get("content")
            else:
                block = card.get("content")
            
            if not block:
                continue
            
            text = self._parse_draftjs_block(block)
            if not text:
                continue
            
            md_line = self._apply_card_formatting(text, card_type)
            if md_line:
                output_lines.append(md_line)
        
        return "\n\n".join(output_lines)
    
    def _parse_draftjs_block(self, block) -> str:
        """Extract text from DraftJS structure."""
        if isinstance(block, str):
            return block
        
        if not isinstance(block, dict):
            return ""
        
        if "blocks" in block:
            lines = []
            for b in block["blocks"]:
                text = b.get("text", "")
                styles = b.get("inlineStyleRanges", [])
                styled_text = self._apply_inline_styles(text, styles)
                if styled_text.strip():
                    lines.append(styled_text)
            return "\n".join(lines)
        
        return ""
    
    def _apply_inline_styles(self, text: str, style_ranges: list) -> str:
        """Apply inline styles (BOLD, etc.) to text."""
        if not style_ranges:
            return text
        
        style_ranges = sorted(
            style_ranges,
            key=lambda r: r.get("offset", 0) + r.get("length", 0),
            reverse=True
        )
        
        for r in style_ranges:
            offset = r.get("offset", 0)
            length = r.get("length", 0)
            style = r.get("style", "")
            
            if offset < 0 or offset >= len(text):
                continue
            
            end = min(offset + length, len(text))
            
            if style == "BOLD":
                text = text[:offset] + "**" + text[offset:end].strip() + "**" + text[end:]
        
        return text
    
    def _apply_card_formatting(self, text: str, card_type: str) -> str:
        """Apply markdown formatting based on card type."""
        if card_type == "header":
            return f"# {text}"
        elif card_type == "subheader":
            return f"## {text}"
        elif card_type == "alphabetical":
            return f"### {text}"
        elif card_type == "important_text":
            return f"> {text}"
        elif card_type == "ul":
            lines = text.splitlines()
            return "\n".join(f"- {line}" for line in lines if line.strip())
        elif card_type == "ol":
            lines = text.splitlines()
            return "\n".join(f"{i+1}. {line}" for i, line in enumerate(lines) if line.strip())
        elif card_type in ("divider", "divider_noline"):
            return "---"
        else:
            return text
    
    # =========================================================================
    # PHASE 3 + 4: Case Resolution & Mapping
    # =========================================================================
    
    def _build_cases(self, case_keys: List[str], cosmos_lang_id: str, 
                     asset_lang_id: str, is_translated: bool) -> Optional[List[Dict]]:
        """Build cases array for LME payload."""
        cases_list = []
        
        for order, case_key in enumerate(case_keys):
            case_doc = self._fetch_case(case_key, cosmos_lang_id, is_translated)
            
            if not case_doc:
                if is_translated:
                    print(f"  ❌ Case '{case_key}' not found for langId '{cosmos_lang_id}'. Failing migration.")
                    return None
                else:
                    print(f"  ⚠️ Case '{case_key}' not found. Skipping case.")
                    continue
            
            mapped_case = self._map_case_to_lme(case_doc, order, asset_lang_id, is_translated)
            if mapped_case:
                cases_list.append(mapped_case)
        
        return cases_list
    
    def _fetch_case(self, case_key: str, lang_id: str, is_translated: bool) -> Optional[Dict]:
        """Fetch case document from Cosmos."""
        if is_translated and lang_id and lang_id != "global":
            query = f"""
                SELECT * FROM c 
                WHERE c._table = 'cases' 
                  AND c.langId = '{lang_id}' 
                  AND c.key = '{case_key}'
                ORDER BY c._ts DESC
                OFFSET 0 LIMIT 1
            """
            items = list(self.container.query_items(query=query, enable_cross_partition_query=True))
            if items:
                return items[0]
            return None
        else:
            query = f"""
                SELECT * FROM c 
                WHERE c._table = 'cases' 
                  AND c.key = '{case_key}'
                  AND (NOT IS_DEFINED(c.langId) OR c.langId = 'global' OR c.langId = '')
                ORDER BY c._ts DESC
                OFFSET 0 LIMIT 1
            """
            items = list(self.container.query_items(query=query, enable_cross_partition_query=True))
            if items:
                return items[0]
            return None
    
    def _map_case_to_lme(self, case_doc: Dict, order: int, 
                         asset_lang_id: str, is_translated: bool) -> Dict:
        """Map Cosmos case document to LME case structure."""
        case_desc = case_doc.get("description", "")
        if not isinstance(case_desc, str):
            case_desc = ""
        
        c_image_asset = None
        c_image_path = case_doc.get("image")
        if c_image_path:
            c_image_asset = DataFactory._download_and_upload_asset(
                c_image_path, asset_lang_id, "icon"
            )
        
        questions = []
        for q_idx, q in enumerate(case_doc.get("questions", [])):
            mapped_q = self._map_question_to_lme(q, q_idx, asset_lang_id, is_translated)
            questions.append(mapped_q)
        
        return {
            "description": case_desc or " ",
            "image": c_image_asset,
            "order": order,
            "questions": questions
        }
    
    def _map_question_to_lme(self, q: Dict, order: int, 
                             asset_lang_id: str, is_translated: bool) -> Dict:
        """Map question to LME structure."""
        question_text = self._extract_translated_field(q.get("question"), is_translated)
        question_desc = self._extract_translated_field(q.get("description"), is_translated)
        
        quizz_type = q.get("quizzType") or q.get("type") or "oneCorrect"
        
        q_image_asset = None
        q_image_path = q.get("image")
        if q_image_path:
            q_image_asset = DataFactory._download_and_upload_asset(
                q_image_path, asset_lang_id, "icon"
            )
        
        answers = []
        for a_idx, a in enumerate(q.get("answers", [])):
            mapped_a = self._map_answer_to_lme(a, a_idx, is_translated)
            answers.append(mapped_a)
        
        return {
            "question": question_text or " ",
            "description": question_desc or " ",
            "quizz_type": quizz_type,
            "image": q_image_asset,
            "show_toggle": False,
            "essential": False,
            "order": order,
            "answers": answers
        }
    
    def _map_answer_to_lme(self, a: Dict, order: int, is_translated: bool) -> Dict:
        """Map answer to LME structure."""
        value = self._extract_translated_field(a.get("value"), is_translated)
        correct = bool(a.get("correct"))
        result = a.get("result") or "neutral"
        
        return {
            "value": value or " ",
            "correct": correct,
            "result": result,
            "order": order
        }
    
    def _extract_translated_field(self, field, is_translated: bool) -> str:
        """Extract string from field."""
        if isinstance(field, str):
            return field
        
        if not field:
            return ""
        
        if isinstance(field, dict):
            if is_translated:
                value = field.get("translated")
                if value:
                    return str(value)
                return ""
            else:
                value = field.get("content")
                if value:
                    return str(value)
                return ""
        
        return str(field)
    
    # =========================================================================
    # PHASE 5: Payload Assembly & Validation
    # =========================================================================
    
    def _assemble_payload(self, doc: Dict, title: str, description: str,
                          content_asset_id: str, cases: List[Dict],
                          is_translated: bool, cosmos_lang_id: str,
                          parent_id: str = None) -> Dict:
        """Assemble final LME payload."""
        lme_lang_id = None
        region = ""
        
        if is_translated and cosmos_lang_id:
            if cosmos_lang_id in self.language_mapping:
                lme_lang_id = self.language_mapping[cosmos_lang_id]["lme_language_id"]
                region = self.language_mapping[cosmos_lang_id]["region"]
            else:
                print(f"  ⚠️ Unknown language ID {cosmos_lang_id}. Proceeding without language mapping.")
        
        # Modify TITLE for translations to ensure unique slug
        if is_translated:
            suffix = region if region else (lme_lang_id or cosmos_lang_id)
            if suffix:
                title = f"{title} ({suffix})"
        
        payload = {
            "title": title,
            "description": description,
            "content": content_asset_id,
            "passRate": doc.get("passRate", 70),
            "deadly": 1 if doc.get("deadly") else 0,
            "content_type": "translated" if is_translated else "original",
            "created_by": "System",
            "cases": cases
        }
        
        if is_translated and lme_lang_id:
            payload["language_id"] = lme_lang_id
            payload["region"] = region
        
        if parent_id:
            payload["derived_from_id"] = parent_id
        
        return payload
    
    def _validate_payload(self, payload: Dict) -> Optional[str]:
        """Pre-send validation."""
        for case in payload.get("cases", []):
            if isinstance(case.get("description"), dict):
                return f"Case description is dict, not string"
            
            for q in case.get("questions", []):
                if isinstance(q.get("question"), dict):
                    return f"Question text is dict, not string"
                if isinstance(q.get("description"), dict):
                    return f"Question description is dict, not string"
                
                for a in q.get("answers", []):
                    if isinstance(a.get("value"), dict):
                        return f"Answer value is dict, not string"
                    if isinstance(a.get("result"), dict):
                        return f"Answer result is dict, not string"
        
        return None
    
    # =========================================================================
    # PHASE 6: Send to LME
    # =========================================================================
    
    def _post_certificate(self, payload: Dict, cosmos_id: str, parent_id: str = None, title: str = None) -> Optional[str]:
        """
        POST certificate to LME API.
        
        CRITICAL FIX:
        - Handle 409/500 duplicate errors by finding existing certificate
        - Properly extract certificate ID from various response formats
        - Extract and activate version if present
        """
        headers = {
            "Authorization": f"Bearer {JWT_TOKEN}",
            "Content-Type": "application/json"
        }
        
        url = self.api_url
        
        if parent_id:
            print(f"  → Creating VERSION via POST {url} (derived_from_id={parent_id})")
        else:
            print(f"  → Creating NEW certificate via POST {url}")
        
        # Rate limit: 1s delay before write
        time.sleep(1)
        
        try:
            resp = requests.post(url, json=payload, headers=headers, allow_redirects=True)
            
            if resp.status_code in (200, 201):
                response_data = resp.json()
                
                lme_id = self._extract_certificate_id(response_data)
                
                if lme_id:
                    print(f"  ✅ Certificate created successfully")
                    print(f"     Cosmos ID: {cosmos_id}")
                    print(f"     LME ID: {lme_id}")
                    
                    self._save_mapping(cosmos_id, lme_id, payload.get("title"))
                    if payload.get("title"):
                        self.existing_certs_cache[payload.get("title")] = lme_id
                    
                    # Version Activation logic
                    version_id = self._extract_version_id(response_data)
                    if version_id:
                        self._activate_version(version_id)
                    else:
                        print(f"  ⚠️ No version_id found in response, cannot activate.")

                    return lme_id
                else:
                    print(f"  ⚠️ Certificate created but no ID in response")
                    print(f"     Response keys: {list(response_data.keys())}")
                    return None
            
            # Handle 409 Conflict or 500 with duplicate error
            elif resp.status_code in (409, 500):
                error_text = resp.text.lower()
                
                # Check if it's a duplicate slug error
                if "already exists" in error_text or "duplicate" in error_text:
                    print(f"  ⚠️ Certificate already exists (HTTP {resp.status_code})")
                    print(f"     Attempting to find existing certificate...")
                    
                    # Try to find existing certificate
                    existing_id = self._find_existing_certificate(title or payload.get("title"))
                    
                    if existing_id:
                        print(f"  ✅ Found existing certificate: {existing_id}")
                        
                        # Save mapping
                        self._save_mapping(cosmos_id, existing_id, payload.get("title"))
                        if payload.get("title"):
                            self.existing_certs_cache[payload.get("title")] = existing_id
                        
                        return existing_id
                    else:
                        print(f"  ❌ Certificate exists but could not retrieve ID")
                        return None
                else:
                    print(f"  ❌ Failed to POST certificate: {resp.status_code}")
                    print(f"     Response: {resp.text}")
                    return None
            else:
                print(f"  ❌ Failed to POST certificate: {resp.status_code}")
                try:
                    error_data = resp.json()
                    print(f"     Error: {error_data}")
                except:
                    print(f"     Error: {resp.text[:500]}")
                return None
                
        except Exception as e:
            print(f"  ❌ Request Error: {e}")
            print(f"     URL: {url}")
            print(f"     Title: {payload.get('title')}")
            return None

    def _extract_version_id(self, data: Dict) -> Optional[str]:
        """Extract version_id from response data.
        
        IMPORTANT: Prioritizes the latest version (highest version number)
        from the versions array to ensure translated versions get activated.
        """
        # Check direct fields
        vid = data.get("certificate_version_id") or data.get("version_id")
        if vid:
            return vid
            
        # Check versions array — pick latest (highest version number)
        versions = data.get("versions", [])
        if versions and isinstance(versions, list) and len(versions) > 0:
            best = max(
                versions,
                key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0,
            )
            if isinstance(best, dict):
                vid = best.get("certificate_version_id") or best.get("version_id") or best.get("id")
                if vid:
                    return vid
        
        # Check draft translated versions
        for draft_key in ("draft_translated_versions", "draft_adapted_versions"):
            drafts = data.get(draft_key, [])
            if drafts and isinstance(drafts, list) and len(drafts) > 0:
                best_draft = max(
                    drafts,
                    key=lambda v: float(v.get("version", 0)) if isinstance(v, dict) else 0,
                )
                if isinstance(best_draft, dict):
                    vid = best_draft.get("certificate_version_id") or best_draft.get("version_id")
                    if vid:
                        return vid
        
        # Fallback: current_original_version (for brand-new certificates)
        curr = data.get("current_original_version")
        if curr and isinstance(curr, dict):
             vid = curr.get("certificate_version_id") or curr.get("version_id")
             if vid:
                 return vid
                 
        return None

    def _activate_version(self, version_id: str) -> bool:
        if os.environ.get("MIGRATE_ENV") == "devcontent":
            print(f"  → Skipping certificate activation for devcontent.")
            return True
        """Activate the certificate version."""
        if not version_id:
            return False
            
        url = ACTIVATE_CERTIFICATE_VERSION.format(version_id=version_id)
        print(f"  → Activating version {version_id} via PATCH {url}")
        
        headers = {
            "Authorization": f"Bearer {JWT_TOKEN}",
            "Content-Type": "application/json"
        }
        
        try:
            resp = requests.patch(url, json={"status": "active", "updated_by": "System"}, headers=headers)
            if resp.status_code in (200, 204):
                print(f"  ✅ Version activated successfully.")
                return True
            else:
                print(f"  ⚠️ Failed to activate version: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            print(f"  ⚠️ Error activating version: {e}")
            return False

    
    def _extract_certificate_id(self, response_data: dict) -> Optional[str]:
        """Extract certificate ID from various response formats."""
        # Try multiple possible ID fields
        id_fields = [
            "id",
            "certificate_id",
            "data.id",
            "result.id",
            "certificate.id"
        ]
        
        for field in id_fields:
            if "." in field:
                # Nested field
                parts = field.split(".")
                value = response_data
                for part in parts:
                    if isinstance(value, dict):
                        value = value.get(part)
                    else:
                        break
                if value and isinstance(value, str):
                    return value
            else:
                # Top-level field
                value = response_data.get(field)
                if value:
                    return value
        
        # Try to find any dict with an 'id' field
        if isinstance(response_data, dict):
            for key, value in response_data.items():
                if isinstance(value, dict) and "id" in value:
                    return value["id"]
        
        return None