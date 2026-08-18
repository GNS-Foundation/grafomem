# Developer convenience targets. See README → "Secret scanning".
.PHONY: secret-hooks secret-scan secret-corpus

# Enable the local pre-commit secret scanner (once per clone). Requires: gitleaks.
secret-hooks:
	git config core.hooksPath .githooks
	@echo "✅ pre-commit secret scanning enabled (.githooks/pre-commit). Needs: gitleaks."

# Scan your STAGED changes locally with the grafomem rules (same as the hook does).
secret-scan:
	gitleaks git --staged --config .gitleaks.toml --no-banner --redact

# Corpus self-test: assert the rules catch every synthetic positive and flag no negative.
secret-corpus:
	python3 tests/secret_scanner/run_corpus_check.py
