.PHONY: dev test trigger logs install check clean

install:
	pip install -r requirements.txt

dev:
	@echo "Starting webhook server on port 8000..."
	@echo "Run 'ngrok http 8000' in a separate terminal to expose it"
	python main.py

test:
	pytest tests/ -v --tb=short

test-local:
	python test_local.py

trigger:
	@echo "Sending fake webhook event to local server..."
	python cli.py trigger --run-id test-$(shell date +%s)

logs:
	@ls -1t logs/*.jsonl 2>/dev/null | head -1 | xargs -I{} python -c \
	"import json,sys; [print(json.dumps(json.loads(l), indent=2)) for l in open('{}')]" 2>/dev/null || \
	echo "No logs yet. Run 'make dev' and trigger a pipeline failure."

status:
	python cli.py status

optimize:
	python cli.py optimize

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -name "*.pyc" -delete 2>/dev/null; \
	rm -rf .pytest_cache .ruff_cache; \
	echo "Cleaned."

check:
	@python -c "import google.genai; print('✓ google-genai')" 2>/dev/null || echo "✗ google-genai missing — run: pip install google-genai"
	@python -c "import fastapi; print('✓ fastapi')" 2>/dev/null || echo "✗ fastapi missing"
	@python -c "import mcp; print('✓ mcp')" 2>/dev/null || echo "✗ mcp missing"
	@python -c "import networkx; print('✓ networkx')" 2>/dev/null || echo "✗ networkx missing"
	@python -c "import yaml; print('✓ pyyaml')" 2>/dev/null || echo "✗ pyyaml missing"
	@[ -f .env ] && echo "✓ .env exists" || echo "✗ .env missing — copy .env.example to .env and fill in values"
