.PHONY: install demo dev test gateway serve tiers report onboard lint
install:
	pip install -e ".[dev]"
demo:
	python -m abenlux.cli demo
dev:
	python scripts/dev.py
test:
	pytest -q
gateway:
	python -m abenlux.cli gateway --port 8088
serve:
	python -m abenlux.cli serve --port 8090
tiers:
	python -m abenlux.cli tiers
report:
	python -m abenlux.cli report
onboard:
	python -m abenlux.cli onboard
lint:
	ruff check src tests
