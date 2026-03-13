PACKAGE_MANAGER = uv
# Format code with black

run:
	$(PACKAGE_MANAGER) run python main.py
format:
	$(PACKAGE_MANAGER) run black . 
	$(PACKAGE_MANAGER) run isort .

# Lint code with flake8
lint:
	$(PACKAGE_MANAGER) run flake8 $(APP_DIR) $(TEST_DIR) --exclude .venv