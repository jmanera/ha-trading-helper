.PHONY: help install check lint typecheck test test-all security run scan validate journal up down app-bundle wear wear-install clean

UV ?= uv

help:  ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:  ## Install dependencies and git hooks
	$(UV) sync --frozen
	$(UV) run pre-commit install

lint:  ## Ruff lint + format check
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

typecheck:  ## mypy --strict
	$(UV) run mypy src

test:  ## Unit + property tests with coverage gates
	$(UV) run pytest --cov --cov-report=term --cov-report=xml
	$(UV) run coverage report --include='*/indicators/*,*/strategy/*' --fail-under=100

test-all:  ## Also run integration tests (needs Docker)
	$(UV) run pytest -m "" --cov --cov-report=term

security:  ## bandit + pip-audit
	$(UV) run bandit -q -r src
	$(UV) export --frozen --no-dev --no-emit-project -o .audit-requirements.txt >/dev/null
	$(UV) run pip-audit -r .audit-requirements.txt --disable-pip
	@rm -f .audit-requirements.txt

check: lint typecheck test security  ## Everything CI runs (except Docker jobs)

scan:  ## Score the universe once and print the ranking
	$(UV) run ha-trading-helper scan

validate:  ## Walk-forward validation (2 years)
	$(UV) run ha-trading-helper validate

run:  ## Long-running service (needs an MQTT broker)
	$(UV) run ha-trading-helper serve

journal:  ## Journal web panel + sync on http://localhost:8099 (needs OKX read-only keys)
	$(UV) run ha-trading-helper journal serve

up:  ## Mosquitto + Home Assistant + app with Docker Compose
	docker compose up --build -d

down:
	docker compose down

app-bundle:  ## Self-contained app folders for /addons (/local_apps) on Home Assistant
	for app in ha_trading_helper:trading_helper intraday_monitor:intraday_monitor okx_journal:okx_journal; do \
		out=dist/$${app%%:*}; src=$${app##*:}; \
		rm -rf $$out && mkdir -p $$out && cp -r $$src/. $$out/ && \
		cp -r pyproject.toml uv.lock README.md src $$out/ && \
		find $$out -name '__pycache__' -prune -exec rm -rf {} + ; \
	done
	@echo "Copy dist/ha_trading_helper, dist/intraday_monitor and dist/okx_journal to /addons/ on Home Assistant"

WEAR_APK = wear/okx-journal-face/build/outputs/apk/release/okx-journal-face-release.apk

wear:  ## Build the Wear OS apps in wear/ (needs JDK 17+ and the Android SDK)
	cd wear && ./gradlew assembleRelease

wear-install: wear  ## Install the Bitácora OKX watch face on a watch paired with adb (see wear/README.md)
	adb install -r $(WEAR_APK)

clean:
	rm -rf dist wear/*/build .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
