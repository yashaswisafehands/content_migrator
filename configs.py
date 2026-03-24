import os
from dotenv import load_dotenv

load_dotenv()

JWT_TOKEN = os.getenv("JWT_TOKEN")

COSMOS_ENDPOINT = "https://sdacms.documents.azure.com:443/"
COSMOS_KEY = os.getenv("COSMOS_KEY")
DATABASE_NAME = "production"
CONTAINER_NAME = "content"
def _get_assets_base_url() -> str:
    env = os.environ.get("MIGRATE_ENV", "content")
    return f"https://sdacms.blob.core.windows.net/{env}/assets/"

ASSETS_BASE_URL = _get_assets_base_url()

LME_BASE_URL = "http://135.225.105.160:8004"

POST_LANGUAGE = f"{LME_BASE_URL}/languages/"

POST_MODULE = f"{LME_BASE_URL}/modules/"
PATCH_MODULE = f"{LME_BASE_URL}/modules/{{module_id}}"
ACTIVATE_MODULE_VERSION = f"{LME_BASE_URL}/modules/version/{{version_id}}/status"

POST_RESOURCE = f"{LME_BASE_URL}/resources/{{tag}}/"
UPDATE_RESOURCE = f"{LME_BASE_URL}/resources/{{resource_id}}"
ACTIVATE_RESOURCE_VERSION = f"{LME_BASE_URL}/resources/version/{{version_id}}/status"

POST_KLP = f"{LME_BASE_URL}/klps/"
UPDATE_KLP = f"{LME_BASE_URL}/klps/{{klp_id}}"
ACTIVATE_KLP_VERSION = f"{LME_BASE_URL}/klps/versions/{{version_id}}/status"

POST_ASSET = f"{LME_BASE_URL}/assets/{{tag}}/upload"

POST_CERTIFICATE = f"{LME_BASE_URL}/certificates/"
ACTIVATE_CERTIFICATE_VERSION = f"{LME_BASE_URL}/certificates/versions/{{version_id}}/status"

UNIVERSAL_ACTIVATE_VERSION = f"{LME_BASE_URL}/manage/version/{{version_id}}/activate"
