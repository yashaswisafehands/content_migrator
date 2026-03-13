import json

with open("module_list.json", encoding="utf-8") as f:
    module_list_keys = json.load(f)

with open(r"c:\Users\vikra\Developer\dev\content-bundle-marathi.json", encoding="utf-8") as f:
    bundle = json.load(f)

bundle_modules = bundle.get("modules", [])
bundle_keys = [m.get("id", "") for m in bundle_modules]

results = {
    "module_list_count": len(module_list_keys),
    "bundle_count": len(bundle_keys),
    "module_list_keys": sorted(module_list_keys),
    "bundle_keys": sorted(bundle_keys),
    "in_static_not_bundle": sorted(set(module_list_keys) - set(bundle_keys)),
    "in_bundle_not_static": sorted(set(bundle_keys) - set(module_list_keys)),
    "in_both": sorted(set(module_list_keys) & set(bundle_keys)),
}

# Add descriptions for bundle modules
results["bundle_modules_detail"] = [
    {"id": m.get("id"), "description": m.get("description", "")} 
    for m in bundle_modules
]

with open("compare_output.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

print("Done. Results in compare_output.json")
