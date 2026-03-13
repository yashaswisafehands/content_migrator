import sys
import json
from configs import get_cosmos_client
from factories import DataFactory

client, container = get_cosmos_client()

data_loc = DataFactory.get_action_card_data(client, container, 'management-hypertension-new-2025-_1763556866355', '7cf6efab-a9d7-54d2-2cbd-ec82efe4a7da')
data_glob = DataFactory.get_action_card_data(client, container, 'management-hypertension-new-2025-_1763556866355', '')

with open('c:/Users/vikra/Developer/dev/jsons/debug_card_loc.json', 'w', encoding='utf-8') as f:
    json.dump(data_loc, f, indent=2, ensure_ascii=False) if data_loc else f.write('{}')

with open('c:/Users/vikra/Developer/dev/jsons/debug_card_glob.json', 'w', encoding='utf-8') as f:
    json.dump(data_glob, f, indent=2, ensure_ascii=False) if data_glob else f.write('{}')

print(f"Local: {bool(data_loc)}, Global: {bool(data_glob)}")
