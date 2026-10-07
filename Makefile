.PHONY: install test synth deploy destroy demo-local

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'

test:
	. .venv/bin/activate && pytest

synth:
	. .venv/bin/activate && cd infra && npx --yes aws-cdk@2 synth

deploy:
	. .venv/bin/activate && cd infra && npx --yes aws-cdk@2 deploy --require-approval never --outputs-file ../cdk-outputs.json

destroy:
	. .venv/bin/activate && cd infra && npx --yes aws-cdk@2 destroy --force

demo-local:
	. .venv/bin/activate && bash scripts/demo_local.sh
