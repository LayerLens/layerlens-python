#!/usr/bin/env bash
#
# Tier-1 mutation score (LAY-3572). Runs `mutmut` per important-corner module
# with a SCOPED test runner (so each mutant runs only the relevant tests and the
# job stays tractable) and reports a per-module mutation score + surviving-mutant
# list. REPORT-ONLY by default: it emits ::warning:: for modules below the soft
# floor but exits 0, so the nightly job never red-flags on a flaky baseline.
# Set MUTATION_ENFORCE=1 to make it exit non-zero below the floor (future ratchet).
#
# A module is scored only on its whole scoped suite. When the precheck fails, or
# SKIPS any test (an optional SDK or framework missing from this environment),
# the module gets a ::warning:: and no score, as the adapter matrix fails a row
# on any skip. MUTATION_MODULES (space-separated names; unset or empty: every
# module) runs a subset, so the nightly job can score each module in the
# environment where its whole suite runs. `--list` prints the module names.
#
# Requires: mutmut (2.5.x — parso-based, no Rust build), pytest, pytest-cov,
# sqlite3, and an editable install of the package. The nightly workflow installs
# these. Run locally with:  bash scripts/mutation_tier1.sh
#
set -uo pipefail

PY="${PYTHON:-python}"
MUT="${MUTMUT:-mutmut}"
PT="$PY -m pytest -x -q --no-header -p no:cacheprovider --assert=plain"
FLOOR="${MUTATION_FLOOR:-50}"          # soft per-module floor (% killed)
ENFORCE="${MUTATION_ENFORCE:-0}"
SRC=src/layerlens/instrument
ASRC=src/layerlens/attestation
PKG=layerlens.instrument

# name | file(to mutate) | dotted module(for --cov) | scoped tests
MODULES=(
  "pricing|$SRC/adapters/providers/pricing.py|$PKG.adapters.providers.pricing|tests/instrument/adapters/providers/test_pricing.py tests/instrument/adapters/frameworks/test_cost_usd_fire.py"
  "secret_scrub|$SRC/_secret_scrub.py|$PKG._secret_scrub|tests/instrument/test_secret_scrub.py tests/instrument/adapters/protocols/test_no_content_sweep.py"
  "capture_config|$SRC/_capture_config.py|$PKG._capture_config|tests/instrument/test_capture_config.py tests/instrument/test_redaction_backstop.py tests/instrument/test_content_keys_guard.py tests/instrument/test_layer_suppression.py tests/instrument/test_event_schema.py tests/instrument/adapters/protocols/test_no_content_sweep.py tests/instrument/adapters/protocols/test_protocol_redaction.py"
  "collector|$SRC/_collector.py|$PKG._collector|tests/instrument/test_redaction_backstop.py tests/instrument/adapters/protocols/test_no_content_sweep.py tests/instrument/test_secret_scrub.py tests/instrument/test_trace_context.py tests/attestation/test_integration.py"
  "upload|$SRC/_upload.py|$PKG._upload|tests/instrument/test_upload.py"
  "chain|$ASRC/_chain.py|layerlens.attestation._chain|tests/attestation/"
  "verify|$ASRC/_verify.py|layerlens.attestation._verify|tests/attestation/"
  "ap2|$SRC/adapters/protocols/ap2.py|$PKG.adapters.protocols.ap2|tests/instrument/adapters/protocols/test_ap2_payment.py tests/instrument/adapters/protocols/test_ap2_expiry.py tests/instrument/adapters/protocols/test_protocol_redaction.py tests/instrument/adapters/protocols/test_no_content_sweep.py tests/instrument/adapters/protocols/test_certification.py"
  "a2ui|$SRC/adapters/protocols/a2ui.py|$PKG.adapters.protocols.a2ui|tests/instrument/adapters/protocols/test_protocol_redaction.py tests/instrument/adapters/protocols/test_certification.py tests/instrument/adapters/protocols/test_no_content_sweep.py"
  "ucp|$SRC/adapters/protocols/ucp.py|$PKG.adapters.protocols.ucp|tests/instrument/adapters/protocols/test_protocol_redaction.py tests/instrument/adapters/protocols/test_no_content_sweep.py tests/instrument/adapters/protocols/test_certification.py"
  "emit_helpers|$SRC/adapters/providers/_emit_helpers.py|$PKG.adapters.providers._emit_helpers|tests/instrument/adapters/providers/"
)

if [ "${1:-}" = "--list" ]; then
  for entry in "${MODULES[@]}"; do echo "${entry%%|*}"; done
  exit 0
fi

# A name that is not a module is an error, so a typo cannot quietly score nothing.
selected=()
for want in ${MUTATION_MODULES:-}; do
  found=0
  for entry in "${MODULES[@]}"; do
    if [ "${entry%%|*}" = "$want" ]; then
      selected+=("$entry")
      found=1
    fi
  done
  if [ "$found" -eq 0 ]; then
    echo "::error title=mutation::unknown module '$want' in MUTATION_MODULES (bash $0 --list prints them)"
    exit 2
  fi
done
if [ "${#selected[@]}" -eq 0 ]; then
  selected=("${MODULES[@]}")
fi

rm -f .mutmut-cache

for entry in "${selected[@]}"; do
  IFS='|' read -r name file dotted tests <<< "$entry"
  echo "::group::mutation $name"
  rm -f .coverage
  pre_log="/tmp/mut_$name.pre.log"
  if ! $PY -m pytest -q -rs -p no:cacheprovider --no-header --cov="$dotted" --cov-report= $tests </dev/null > "$pre_log" 2>&1; then
    echo "::warning title=mutation $name::precheck failed (scoped tests not green: $(tail -n 1 "$pre_log")) — skipping"
    echo "::endgroup::"; continue
  fi
  # A skipped test is part of the module's suite that did not run here, so a
  # score would describe a smaller suite than the module's. Whole suite or no score.
  summary="$(tail -n 1 "$pre_log")"
  if grep -Eq '[0-9]+ skipped' <<< "$summary"; then
    echo "::warning title=mutation $name::precheck skipped tests ($summary) — not scored on a partial suite"
    grep '^SKIPPED' "$pre_log" || true
    echo "::endgroup::"; continue
  fi
  $MUT run --paths-to-mutate "$file" --runner "$PT $tests" --use-coverage --simple-output --no-progress --CI </dev/null > "/tmp/mut_$name.run.log" 2>&1 || true
  echo "::endgroup::"
done

# Per-module score from the accumulated cache. sqlite3 sums each file's statuses,
# so this needs no associative arrays and runs on any bash (macOS ships 3.2); the
# rows are read in THIS shell, not a pipe subshell, so the enforce-fail flag holds.
fail=0
{
  echo "### Tier-1 mutation score (LAY-3572)${MUTATION_MODULES:+ — ${MUTATION_MODULES}}"
  echo ""
  echo "| module | killed | survived | score |"
  echo "| --- | ---: | ---: | ---: |"
} | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"

if [ -f .mutmut-cache ]; then
  while IFS='|' read -r f k s; do
    [ -z "${f:-}" ] && continue
    tot=$((k + s))
    [ "$tot" -eq 0 ] && continue
    score=$((100 * k / tot))
    printf '| %s | %d | %d | %d%% |\n' "$f" "$k" "$s" "$score" | tee -a "${GITHUB_STEP_SUMMARY:-/dev/null}"
    if [ "$score" -lt "$FLOOR" ]; then
      echo "::warning title=mutation floor::$f mutation score ${score}% < floor ${FLOOR}%"
      [ "$ENFORCE" = "1" ] && fail=1
    fi
  done < <(sqlite3 .mutmut-cache "SELECT sf.filename, sum(m.status = 'ok_killed'), sum(m.status = 'bad_survived') FROM Mutant m JOIN Line l ON m.line = l.id JOIN SourceFile sf ON l.sourcefile = sf.id GROUP BY sf.filename ORDER BY sf.filename;")
fi

exit $fail
