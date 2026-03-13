"""Global content migration functionality."""

from typing import Dict, List, Any
from azure.cosmos import CosmosClient

from module_migrator import ModuleMigrator
from resource_migrator import ResourceMigrator
from factories import DataFactory
from data_models import ResourcePostRequestData
from slug_utils import build_slug, slugify
from certificate_migrator import CertificateMigrator
from settings_screen_migrator import SettingsScreenMigrator


class GlobalContentMigrator:
    """Handles migration of ALL global content (modules, resources) where langId=''."""

    def __init__(
        self,
        cosmos_client: CosmosClient,
        container,
    ):
        self.cosmos_client = cosmos_client
        self.container = container
        # Use empty language mapping for global contexts
        self.language_mapping = {} 
        
        self.resource_migrator = ResourceMigrator(cosmos_client, container, self.language_mapping)
        self.module_migrator = ModuleMigrator(cosmos_client, container, self.language_mapping)
        self.certificate_migrator = CertificateMigrator(cosmos_client, container, self.language_mapping)
        self.settings_screen_migrator = SettingsScreenMigrator(container, self.language_mapping)

    def migrate_all_global_content(self) -> None:
        """Queue all global content (Stage 1)."""
        print("🚀 Starting GLOBAL content migration (queue stage)...")
        
        # 1. Migrate Global Modules
        self._migrate_global_modules()

        # 2. Migrate Global Resources (Independent Scan)
        
        # --- FIX: ACTION CARDS (Add the CamelCase check) ---
        self._migrate_global_table("action-cards", "action-card")
        self._migrate_global_table("actionCards", "action-card")  # <--- NEW LINE: Catch 'actionCards'
        
        # --- FIX: PROCEDURES (Add the CamelCase check) ---
        self._migrate_global_table("procedures", "procedure")
        self._migrate_global_table("practical-procedures", "procedure") # <--- NEW LINE: Catch 'practical-procedures'
        self._migrate_global_table("practicalProcedures", "procedure")  # Added camelCase just in case
        
        # Drugs (Usually 'drugs' is consistent, but adding both doesn't hurt)
        self._migrate_global_table("drugs", "drug")
        
        # KLPs (This was already working because it had both!)
        self._migrate_global_table("key-learning-points", "key-learning-point")
        self._migrate_global_table("keyLearningPoints", "key-learning-point")

        print("Global migration queueing complete.")

    def _migrate_global_modules(self) -> None:
        """Query and queue global modules from Cosmos DB."""
        print("Scanning global modules...")
        
        # Query for global modules (no langId or empty langId)
        query = (
            "SELECT * FROM c WHERE c._table='modules' "
            "AND (NOT IS_DEFINED(c.langId) OR c.langId = '')"
        )
        try:
            results = list(self.container.query_items(query=query, enable_cross_partition_query=True))
            print(f"Found {len(results)} active global modules in Cosmos DB.")
            
            migrated_count = 0
            for mod in results:
                try:
                    self.module_migrator._migrate_single_module(mod)
                    migrated_count += 1
                except Exception as e:
                    print(f"Error migrating global module {mod.get('id')}: {e}")
                    
            print(f"Successfully queued {migrated_count}/{len(results)} global modules.")
        except Exception as e:
            print(f"Error querying global modules from Cosmos: {e}")

    def _migrate_global_table(self, table_name: str, resource_tag: str) -> None:
        """Query specific table for global items and queue them as resources."""
        print(f"Scanning global {table_name}...")
        query = f"SELECT * FROM c WHERE c._table='{table_name}' AND (NOT IS_DEFINED(c.langId) OR c.langId = '')"
        items = list(self.container.query_items(query=query, enable_cross_partition_query=True))
        print(f"Found {len(items)} global {table_name}.")

        for doc in items:
            try:
                self._process_single_global_resource(doc, resource_tag, table_name)
            except Exception as e:
                print(f"Error migrating global {resource_tag} {doc.get('id')}: {e}")

    def _process_single_global_resource(self, doc: Dict[str, Any], tag: str, table_name: str) -> None:
        """Convert a global doc to a resource and queue it."""
        # Use Factory to create standard Resource Data
        # We need to distinguish type logic similar to ResourceMigrator
        
        key = doc.get("id")
        
        if tag == "action-card":
            # Action Cards
            # Ensure no language ID interferes
            doc_clean = doc.copy()
            doc_clean.pop("langId", None)
            doc_clean.pop("language_id", None)
            
            resources = DataFactory.create_action_card_resources(
                doc_clean, language_id="", allowed_versions=["original"]
            )
            for res in resources:
                self.resource_migrator._create_or_update_complex_resource(
                    res, key, tag, cosmos_language_id=""
                )

        elif tag in ["procedure", "drug"]:
             # Procedures and Drugs (treated like Action Cards in factory)
             # but with specific type override in factory call ideally?
             # DataFactory.create_action_card_resources handles structure.
             # We just need to ensure correct handling directly here.
             
             # Re-using ResourceMigrator logic?
             # ResourceMigrator._migrate_procedure_resources logic:
             resources = DataFactory.create_action_card_resources(
                doc, language_id="", resource_type=tag, allowed_versions=["original"]
             )
             for res in resources:
                 self.resource_migrator._create_or_update_complex_resource(
                    res, key, tag, cosmos_language_id=""
                 )

        elif tag == "key-learning-point":
             # KLPs
             # Logic matches _migrate_key_learning_point_resources from ResourceMigrator
             # But stripped down for single item
             level = str(doc.get("level", "1"))
             
             # Ensure no language ID interferes
             doc_clean = doc.copy()
             doc_clean.pop("langId", None)
             doc_clean.pop("language_id", None)

             resource_data = DataFactory.create_resource_data(doc_clean, table_name, language_id="")
             resource_data.level = level
             resource_data.content_type = "original"
             
             # Slug generation logic
             title = doc.get("title") or doc.get("description") or "Untitled"
             # Clean title logic from ResourceMigrator
             import re
             cleaned_title = re.sub(r"\s*\((?:adapted|translated|original)\)\s*", " ", title, flags=re.IGNORECASE).strip()
             title_slug = slugify(cleaned_title)
             slug = build_slug("klp", level, title_slug)
             
             # Use _queue_klp_post for KLPs (separate from resources in LME)
             questions = resource_data.questions if hasattr(resource_data, 'questions') and resource_data.questions else []
             
             # Convert path-style links to slugs for direct lookup during POST
             for q in questions:
                 if "link" in q and q["link"]:
                     slug_link = self.resource_migrator._convert_link_to_slug(q["link"])
                     if slug_link:
                         q["link"] = slug_link
                     else:
                         # Remove unrecognized links
                         del q["link"]
             
             self.resource_migrator._queue_klp_post(
                 slug=slug,
                 title=resource_data.title,
                 description=resource_data.description or "",
                 level=level,
                 content_type=resource_data.content_type,
                 language_id="",
                 region="",
                 created_by=resource_data.created_by or "System",
                 cosmos_language_id="",
                 questions=questions,
             )

    def post_global_content(self, start_from: str = "resources") -> None:
        """Stage 2: Post content with optional start_from to skip steps.
        
        Steps: resources -> klps -> modules -> certificates -> settings_screens -> onboarding
        """
        steps = ["resources", "klps", "modules", "certificates", "settings_screens", "onboarding"]
        try:
            start_index = steps.index(start_from)
        except ValueError:
            start_index = 0
        
        print(f"🌍 Global Stage 2: Posting from '{start_from}' onwards...")
        
        if start_index <= 0:
            print("Posting global resources...")
            self.resource_migrator.post_resources_from_csv()
        else:
            print("Posting global resources... (SKIPPED)")
        
        if start_index <= 1:
            print("Posting global KLPs...")
            self.resource_migrator.post_klps_from_csv()
        else:
            print("Posting global KLPs... (SKIPPED)")
        
        if start_index <= 2:
            print("Posting global modules...")
            self.module_migrator.post_modules_from_csv()
        else:
            print("Posting global modules... (SKIPPED)")
        
        if start_index <= 3:
            print("Posting global certificates...")
            self.certificate_migrator.migrate_certificates(language_id=None)
        else:
            print("Posting global certificates... (SKIPPED)")
        
        if start_index <= 4:
            print("Posting global settings screens (About)...")
            self.settings_screen_migrator.migrate_global_about_screen()
        else:
            print("Posting global settings screens... (SKIPPED)")

        if start_index <= 5:
            print("\n=== Posting Global Onboarding Flow ===")
            from onboarding_migrator import OnboardingMigrator
            onboarding_mig = OnboardingMigrator()
            flow_id = onboarding_mig.migrate_global()
            if flow_id:
                print(f"  ✅ Global onboarding flow created: {flow_id}")
            else:
                print("  ❌ Global onboarding flow creation failed.")
        else:
            print("Posting global onboarding flow... (SKIPPED)")
