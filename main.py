"""Main entry point for Cosmos DB to LME content migration."""

import argparse
import os

from azure.cosmos import CosmosClient

from configs import CONTAINER_NAME, COSMOS_ENDPOINT, COSMOS_KEY, DATABASE_NAME
from csv_resource_map import load_module_resource_map
from language_migrator import LanguageMigrator
from module_migrator import ModuleMigrator


from category_migrator import CategoryMigrator
from certificate_migrator import CertificateMigrator
from settings_screen_migrator import SettingsScreenMigrator
import configs
import sys
import datetime

# Logger setup to capture output to logs
class Tee:
    def __init__(self, filename, original):
        self.file = open(filename, "a", encoding="utf-8")
        self.original = original

    def write(self, message):
        self.original.write(message)
        self.file.write(message)
        self.file.flush() # Ensure logs are written immediately

    def flush(self):
        self.original.flush()
        self.file.flush()

def setup_logging(stage_name: str):
    """Set up logging to file with timestamp."""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
        
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"{stage_name}_{timestamp}.log")
    
    print(f"📄 Logging output to: {log_file}")
    
    sys.stdout = Tee(log_file, sys.stdout)
    sys.stderr = Tee(log_file, sys.stderr)



class MigrationOrchestrator:
    """Orchestrates the migration workflow."""

    def __init__(self, cosmos_client: CosmosClient, container):
        self.cosmos_client = cosmos_client
        self.container = container

        csv_path = os.path.join(os.path.dirname(__file__), "module_resource_summary.csv")
        self.module_resource_map = load_module_resource_map(csv_path)

        self.language_migrator = LanguageMigrator(cosmos_client, container)
        self.module_migrator = None
        self.module_migrator = None
        self.category_migrator = None
        self.certificate_migrator = None
        self.settings_screen_migrator = None

    def migrate_all_content(
        self,
        *,
        post_stage: bool = False,
        start_from: str = "languages",
        language_id: str = None,
    ) -> None:
        """Run migration in two stages controlled by a flag.

        Stage 1 (default):
          - Read from Cosmos and queue payloads into processed_data CSVs
            languages.csv, modules.csv, resources.csv.

        Stage 2 (--post-stage):
          - Read those CSVs, post to API in order: languages -> resources -> modules.
        """
        if not post_stage:
            print("Starting Stage 1 (queue-only) migration...")
            print("\n=== STEP 1: Language Queue ===")
         
            self.language_migrator.migrate_all_languages(language_id=language_id)

            # Pre-step: Generate module_list.json from content-bundle(s)
            print("\n=== STEP 1.5: Generate Module List from Content Bundle ===")
            from bundle_loader import generate_module_list
            module_keys = generate_module_list()
            if module_keys:
                print(f"Module list ready: {len(module_keys)} modules for target language(s)")
            else:
                print("⚠️  Module list unchanged (using existing module_list.json)")

            print("\n=== STEP 2: Module Queue (includes resources) ===")

            stage_one_language_mapping = (
                self.language_migrator.load_processed_languages_mapping()
            )
            self.module_migrator = ModuleMigrator(
                self.cosmos_client,
                self.container,
                stage_one_language_mapping,
                module_resource_map=self.module_resource_map,
            )
            self.module_migrator.migrate_all_modules(language_filter=language_id)

            print("\n=== STAGE 1 COMPLETE ===")
            print("Queued languages.csv, resources.csv, modules.csv in processed_data")
            return

        # Stage 2: Post from CSVs
        steps = ["languages", "resources", "klps", "modules", "categories", "certificates", "settings_screens", "onboarding"]
        try:
            start_index = steps.index(start_from)
        except ValueError:
            start_index = 0

        print(f"Starting Stage 2 (posting from CSVs), starting from: {start_from}...")

        # Always ensure mappings are loaded and migrators initialized
        self.language_migrator.load_language_mapping()
        language_mapping = self.language_migrator.get_language_mapping()
        self.module_migrator = ModuleMigrator(
            self.cosmos_client, self.container, language_mapping,
            module_resource_map=self.module_resource_map,
        )

        if start_index <= 0:
            print("\n=== STEP 1: Post Languages ===")
            self.language_migrator.post_languages_from_csv()
        else:
             print("\n=== STEP 1: Post Languages (SKIPPED) ===")

        if start_index <= 1:
            print("\n=== STEP 2: Post Resources ===")
            # Ensure placeholders for linked resources (e.g. drugs in KLPs) exist
            print("Ensuring linked resources exist (creating placeholders)...")
            self.module_migrator.resource_migrator._ensure_linked_resources_exist()
            
            # Post resources first to have ids for module payloads
            self.module_migrator.resource_migrator.post_resources_from_csv()
        else:
             print("\n=== STEP 2: Post Resources (SKIPPED) ===")

        if start_index <= 2:
            print("\n=== STEP 3: Post KLPs ===")
            # Post KLPs (separate from resources in LME)
            self.module_migrator.resource_migrator.post_klps_from_csv()
        else:
             print("\n=== STEP 3: Post KLPs (SKIPPED) ===")

        if start_index <= 3:
            print("\n=== STEP 4: Post Modules ===")
            self.module_migrator.post_modules_from_csv()
        else:
             print("\n=== STEP 4: Post Modules (SKIPPED) ===")

        if start_index <= 4:
            print("\n=== STEP 5: Post Categories ===")
            self.category_migrator = CategoryMigrator()
            for cid, info in language_mapping.items():
                lme_lang_id = info.get("lme_language_id")
                if lme_lang_id:
                    self.category_migrator.migrate_categories(lme_lang_id)
        else:
             print("\n=== STEP 5: Post Categories (SKIPPED) ===")

        if start_index <= 5:
            print("\n=== STEP 6: Post Certificates ===")
            self.certificate_migrator = CertificateMigrator(
                self.cosmos_client,
                self.container,
                language_mapping    # Fixed: passed in derived language_mapping
            )
            # Pass language_id filter to migrate_certificates
            self.certificate_migrator.migrate_certificates(language_id=language_id)
        else:
             print("\n=== STEP 6: Post Certificates (SKIPPED) ===")

        if start_index <= 6:
            print("\n=== STEP 7: Post Settings Screens ===")
            
            # Filter Strategy:
            # 1. Get all valid mappings (cosmos_id -> lme_id, loaded from language_mapping.csv)
            # 2. Get target languages (from processed languages.csv)
            # 3. Intersect: Only migrate languages that are in TARGET list AND have valid MAPPING
            
            all_mappings = self.language_migrator.get_language_mapping()
            target_languages_map = self.language_migrator.load_processed_languages_mapping()
            
            filtered_mapping = {}
            for cosmos_id in target_languages_map:
                if cosmos_id in all_mappings:
                    filtered_mapping[cosmos_id] = all_mappings[cosmos_id]
                else:
                    if cosmos_id != "en": # Skip warning for source English if not mapped (usually global)
                         print(f"  ⚠️ Skipping settings for {cosmos_id}: Present in languages.csv but no LME ID found in mapping.")

            print(f"  Filtered settings migration to {len(filtered_mapping)} languages (from languages.csv).")

            self.settings_screen_migrator = SettingsScreenMigrator(
                self.container,
                filtered_mapping
            )
            self.settings_screen_migrator.migrate_about_screen()
        else:
             print("\n=== STEP 7: Post Settings Screens (SKIPPED) ===")

        if start_index <= 7:
            print("\n=== STEP 8: Post Onboarding Flows ===")
            from onboarding_migrator import OnboardingMigrator
            onboarding_mig = OnboardingMigrator()
            
            # Loop through each language and patch its translated onboarding
            target_languages_map = self.language_migrator.load_processed_languages_mapping()
            all_mappings = self.language_migrator.get_language_mapping()
            
            patched = 0
            skipped = 0
            for cosmos_id in target_languages_map:
                if cosmos_id == "en":  # Skip English (already global)
                    continue
                info = all_mappings.get(cosmos_id, {})
                lme_lang_id = info.get("lme_language_id")
                if not lme_lang_id:
                    print(f"  ⚠️ Skipping onboarding for {cosmos_id}: No LME language ID found.")
                    skipped += 1
                    continue
                try:
                    success = onboarding_mig.migrate_translated(
                        cosmos_lang_id=cosmos_id,
                        lme_language_id=lme_lang_id,
                    )
                    if success:
                        patched += 1
                    else:
                        skipped += 1
                except Exception as e:
                    from error_logger import log_error
                    log_error("Captured Exception", exc=e)
                    print(f"  ❌ Error migrating onboarding for {cosmos_id}: {e}")
                    skipped += 1
            print(f"  Onboarding: {patched} patched, {skipped} skipped.")
        else:
             print("\n=== STEP 8: Post Onboarding Flows (SKIPPED) ===")

        print("\n=== STAGE 2 COMPLETE ===")
        print("Languages, resources, KLPs, modules, categories, certificates, settings screens, and onboarding flows posted.")


def main():
    """Main entry point for the migration script."""
    parser = argparse.ArgumentParser(description="Content migration orchestrator")
    parser.add_argument(
        "--post-stage",
        action="store_true",
        help="Run Stage 2: post from CSVs (languages -> resources -> modules)",
    )
    parser.add_argument(
        "--start-from",
        choices=["languages", "resources", "klps", "modules", "categories", "certificates", "settings_screens", "onboarding"],
        default="languages",
        help="Start Stage 2 from a specific step (skips previous steps)",
    )
    parser.add_argument(
        "--language-id",
        help="Filter Stage 1 migration to a specific Cosmos language ID",
    )
    parser.add_argument(
        "--env",
        choices=["content", "devcontent"],
        default="content",
        help="Target environment branch in blob storage (content or devcontent)",
    )
    parser.add_argument(
        "--migrate-global",
        action="store_true",
        help="Migrate GLOBAL data only (langId='') for modules/resources",
    )
    args = parser.parse_args()
    
    os.environ["MIGRATE_ENV"] = args.env
    # Re-evaluate URL constants now that MIGRATE_ENV is set
    # (Both modules evaluate these at import time, before env is available)
    configs.ASSETS_BASE_URL = configs._get_assets_base_url()
    import onboarding_migrator as _obm
    _obm.BLOB_BASE = _obm._get_blob_base()

    if args.migrate_global:
        stage_name = "stage1_global" if not args.post_stage else "stage2_global"
    else:
        stage_name = "stage2" if args.post_stage else "stage1"
    
    setup_logging(stage_name)

    # Initialize Cosmos DB client
    cosmos_client = CosmosClient(COSMOS_ENDPOINT, COSMOS_KEY)
    container = cosmos_client.get_database_client(DATABASE_NAME).get_container_client(
        CONTAINER_NAME
    )

    if args.migrate_global:
        from global_migrator import GlobalContentMigrator
        print("🌍 MODE: Global Data Migration")
        global_migrator = GlobalContentMigrator(cosmos_client, container)
        
        if not args.post_stage:
            # Stage 1: Queue
            global_migrator.migrate_all_global_content()
        else:
            # Stage 2: Post (with optional --start-from)
            global_migrator.post_global_content(start_from=args.start_from)
            
    else:
        # Standard Orchestrator
        orchestrator = MigrationOrchestrator(cosmos_client, container)
        orchestrator.migrate_all_content(
            post_stage=args.post_stage,
            start_from=args.start_from,
            language_id=args.language_id,
        )


if __name__ == "__main__":
    main()
