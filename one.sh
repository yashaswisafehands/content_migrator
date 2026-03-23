#!/bin/bash

set -e   # stop script if any command fails

BASE_PATH="/home/azureuser/test/content_migrator/data/processed_data"

echo "===== GLOBAL MIGRATION ====="
python3 main.py --migrate-global
python3 main.py --migrate-global

echo "Cleaning processed CSVs..."
rm -f $BASE_PATH/resources.csv
rm -f $BASE_PATH/klps.csv
rm -f $BASE_PATH/modules.csv


echo "===== LANGUAGE MIGRATION: 49bc07fa ====="
python3 main.py --language-id 49bc07fa-9ad4-816c-17db-e693a28dd40f
python3 main.py --language-id 49bc07fa-9ad4-816c-17db-e693a28dd40f --post-stage

echo "Cleaning processed CSVs..."
rm -f $BASE_PATH/resources.csv
rm -f $BASE_PATH/klps.csv
rm -f $BASE_PATH/modules.csv


echo "===== LANGUAGE MIGRATION: 7cf6efab ====="
python3 main.py --language-id 7cf6efab-a9d7-54d2-2cbd-ec82efe4a7da
python3 main.py --language-id 7cf6efab-a9d7-54d2-2cbd-ec82efe4a7da --post-stage


echo "===== MIGRATION COMPLETE ====="
