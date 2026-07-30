# taskq-plus verification surface
# NFR-12: `make verify-system` runs the full test suite + CLI smoke
# (submit / run / status / graph / export / clear) and prints
# "verify-system: PASS" on success (exit 0).
#
# See SPEC §4 row 21 and TEST_SPEC.md row 9 (test_nfr12_a) for the
# contract; the corresponding NFR-12 dimension is `execute_verification_target`.

PYTHON     ?= .venv/bin/python
PYTHONPATH ?= 03-development/src
export PYTHONPATH

.PHONY: verify-system test smoke

verify-system: test smoke
	@echo "verify-system: PASS"

test:
	@$(PYTHON) -m pytest 03-development/tests -q --no-header

# CLI smoke — exercises each of the 6 user-visible subcommands called out in
# SPEC §4 row 21. PYTHONPATH is exported above (Make's `export` directive)
# because the package source lives under 03-development/src (src/ layout).
# Each invocation uses a throw-away TASKQ_HOME so the smoke does not pollute
# the developer's working tree. submit returns the generated 8-hex task id on
# stdout, so we capture it (`id_a`, `id_b`) to feed into run/status.
smoke:
	@TASKQ_HOME="$$(mktemp -d -t taskq_smoke.XXXXXX)" && \
	export TASKQ_HOME && \
	id_a=$$($(PYTHON) -m taskq_plus submit --name smoke_a 'echo alpha') && \
	id_b=$$($(PYTHON) -m taskq_plus submit --name smoke_b 'echo beta' --after "$$id_a") && \
	$(PYTHON) -m taskq_plus run "$$id_a" >/dev/null && \
	$(PYTHON) -m taskq_plus status "$$id_a" >/dev/null && \
	$(PYTHON) -m taskq_plus graph >/dev/null && \
	$(PYTHON) -m taskq_plus export --format json >/dev/null && \
	$(PYTHON) -m taskq_plus clear >/dev/null && \
	rm -rf "$$TASKQ_HOME"