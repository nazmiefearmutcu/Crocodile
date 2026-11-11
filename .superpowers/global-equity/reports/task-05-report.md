# Task 05 report: source symbol grammars and tables

Implementers: round 0 (Haiku 5.5) made commit 1 and prepared commit 2's files, then returned
BLOCKED on the task 01 gate edit. Continuation (Opus 5.5, 2026-11-11) resolved the blocker,
re-verified everything and made commit 2. This report covers both.

## Preconditions

- Worktree `crocodile-global-equity`, branch `feat/global-equity`.
- BASE recorded at the start of the continuation: `f9461db` (commit 1 already present on top of
  `ad2fb10`). Working tree at that point: modified script, MANIFEST and
  `tests/conformance/test_markets_reference_data.py`; untracked three grammar CSVs,
  `tests/conformance/test_symbol_grammar.py` and the 12 probe fixtures. Nothing staged.
- Task 01/02 modules (`errors.py` with `NoGrammarForMarket(source, mic)`, `_data.read_csv`,
  `exchanges.py`, `mic.py`, `symbols.listing_symbol`), the build script and MANIFEST exist.
- `git check-ignore -v src/crocodile/core/markets/data/grammar_yahoo_chart.csv` prints nothing
  (exit 1).
- Legacy ruff count: 193 (unchanged).
- `core/markets/__init__.py`, `errors.py`, `_data.py`, `.gitignore`, `.gitattributes`: no diff
  against `769906e`.

## Commits

1. `f9461db feat(markets): add SymbolGrammar with suffix and prefix grammars` (round 0;
   `grammar.py`, `tests/core/markets/test_grammar.py`; trailer Haiku 5.5 per the ledger ruling).
2. `feat(markets): add yahoo_chart, tradingview and google_finance grammar tables` (continuation;
   trailer Opus 5.5, the model that wrote the commit). Paths: the three grammar CSVs,
   `MANIFEST.json`, `scripts/build_markets_reference.py`,
   `tests/conformance/test_symbol_grammar.py`, the 12 probe fixtures, and
   `tests/conformance/test_markets_reference_data.py` (see ruling below).

## RED / GREEN

All commands were run from the worktree root in Git Bash after
`export PYTHONUTF8=1 PYTHONDONTWRITEBYTECODE=1; export PYTHONPATH="$(cygpath -w "$PWD/.superpowers/global-equity/winshim")"; PY=.venv/Scripts/python.exe`.

- Step 1 RED (round 0): `test_grammar.py` failed to import `crocodile.core.markets.grammar`.
  The round-0 transcript is not available to the continuation, so the exact line was not
  recovered. The end state is accepted: commit 1 holds exactly the brief's two paths.
- Step 2 GREEN: `tests/core/markets/test_grammar.py` passes (inside the 657-test run below).
- Step 4 RED, reproduced by the continuation with the two generated CSVs moved away:
  `$PY -m pytest tests/conformance/test_symbol_grammar.py -p no:cacheprovider` gives
  `ERROR tests/conformance/test_symbol_grammar.py - crocodile.core.markets.errors.ReferenceDataError: reference data 'grammar_google_finance.csv' is malformed: packaged file is missing`
  (1 error during collection). The files were restored right after.
- Step 5/6 GREEN:
  `$PY -m pytest -p no:cacheprovider --timeout=1800 tests/core/markets tests/conformance/test_symbol_grammar.py tests/conformance/test_markets_reference_data.py tests/conformance/test_markets_layering.py tests/conformance/test_calendar_cross_checks.py`
  gives `657 passed, 1 warning`.

## Script run

- `$PY scripts/build_markets_reference.py` exit 0 (the project interpreter imports
  `exchange_calendars`; the uv fallback was not needed). Output:
  `yahoo_chart rows skipped (listed, failed or no sample): XADS(failed), XDHA(failed), NEOE(listed), XCNQ(listed), XTSX(listed), BJSE(listed), XBER(listed), XDUS(listed), XHAM(listed), XHAN(listed), XMUN(listed), XICE(failed), XCOL(failed), XCAS(failed), XNSA(failed), XPHS(failed), HSTC(failed)`
  : 9 `listed`, 8 `failed`, no suffix without an example.
- `$PY scripts/build_markets_reference.py --check` exit 0:
  `up to date: iso4217.csv, exchanges.csv, grammar_yahoo_chart.csv, grammar_google_finance.csv, MANIFEST.json`.
- `exchanges.csv` and `iso4217.csv` are byte-identical (no git diff).
- `cmp` of the three committed CSVs against the brief's expected blocks: identical (55 Yahoo,
  3 Google, 13 TradingView rows). All three are ASCII, LF, end with `\n`.
- `cmp` of the 12 fixtures against `.superpowers/global-equity/inputs/tradingview/<m>.json`:
  identical. `ghana.json`, `kazakhstan.json`, `mongolia.json` hold `404 page not found` and were
  not copied.

## Gates and lint

- Clean-package ruff (`src/crocodile/core ... src/stockodile`): All checks passed.
- `ruff check tests/core/markets tests/conformance/test_symbol_grammar.py tests/conformance/test_markets_reference_data.py scripts/build_markets_reference.py`: All checks passed.
- Legacy ruff count: 193 (unchanged, budget 202).
- `$PY -m mypy -p crocodile.core`: Success, no issues found in 84 source files.
- `git ls-files --eol src/crocodile/core/markets/data/` after staging: the three grammar CSVs
  and MANIFEST show `i/lf w/lf attr/-text` (`iso10383_mic.csv` stays `i/crlf`, the verbatim ISO
  input from task 01, unchanged).
- Broad conformance set (brief's second command: test_gates, test_prose_counts,
  test_provider_channels, test_channel_writability, test_record_parity, test_lake_symmetry,
  test_surfaces, test_capability_batches, test_pending_symmetry, test_provenance,
  test_phase1_exit, test_evasion_quarantine, test_ratelimit_placement, test_errors,
  test_import_safety; with `--timeout=1800 -rfE`): `3 failed, 1103 passed, 1 warning in 30.10s`.
  The 3 failures are `test_phase1_exit.py::test_the_deprecated_commands_say_they_are_deprecated`,
  `::test_the_merged_core_is_lint_clean` and `::test_the_legacy_surfaces_do_not_get_worse`; all
  three are in `baseline/summary.json` "bad" (FileNotFoundError, `.venv/bin/python`). No new
  failing id.
- Commit 2 is `3b58ba2`; HEAD after it `3b58ba20cf4e16c1f8624de9feec590e208d4685`; tree clean;
  nothing pushed.

## Ruling taken by the continuation (the BLOCKED item)

**Ruling: `tests/conformance/test_markets_reference_data.py` gains the three grammar files in
its two hard-coded expectations (the exact MANIFEST key set in
`test_every_data_file_matches_its_manifest_sha256` and the `expected_rows` map in
`test_every_manifest_entry_records_hash_rows_date_and_licence`); nothing else in that file
changes.**

- Why: the binding spec (section 3.1, last bullet) requires one `grammar_<source>.csv` per source
  in the packaged data directory, and task 01's gate deliberately pins the exact file set and row
  counts. The brief's two statements ("MANIFEST entries for all three grammar files" and "the
  gate is not edited and stays green") cannot both hold; the spec decides for the files, and the
  gate's purpose (every packaged file is listed and counted) is kept by extending its
  expectations rather than loosening them.
- Cost if wrong: none for behaviour; if the controller prefers the gate to derive counts, it is
  a two-line change.
- Consequence for task 11 (concern): task 11 appends rows to `grammar_tradingview.csv` and its
  brief also says this gate is "unedited". The `"grammar_tradingview.csv": 13` entry will then
  fail. Task 11 must update that one number (or the controller must amend brief 11/11a/11b).

## Other deviations

- `grammar_yahoo_chart_rows(matrix, exchange_mics, skipped=None)`: an optional third parameter
  collects the skipped labels so `main` can print the summary while the builder stays pure. The
  brief's two-argument call (used by the gate) works unchanged.
- The builder also skips a sample equal to the bare suffix (`sample == suffix`), so an empty local
  is never written; no matrix row triggers it.
- `SuffixRow.is_segment` is an extra read-only property (additive, used by the grammar and the
  gate). No interface name was renamed.
- The script's row-count error messages now use the `EXPECTED_*` constants (continuation fix of
  two hard-coded numbers).
- Trailers: commit 1 names Haiku 5.5, commit 2 names Opus 5.5 (ledger ruling: the trailer names
  the model that wrote the commit).

## Protocol-extension ruling (from the brief, implemented)

`to_source(local, mic, *, segment_mic=None)` and `from_source(raw, *, mic=None)`: keyword-only
optional parameters; the spec's positional calls keep working. Needed for KOSDAQ `.KQ` (segment
`XKOS` of `XKRX`) and for unsuffixed US Yahoo symbols (`XNAS`/`XNYS`).

## Notes for later tasks

- **For task 10:** `from_source` on an unsuffixed raw raises `AmbiguousSourceSymbol`. Use
  `.candidates` plus `mic=`, or `to_source` per US MIC.
- **For task 11:** `HSTC` has only `UPCOM`. Nordic class shares (`NOVO-B` on Yahoo, `NOVO_B`
  reportedly on TradingView) will need an explicit rule when Denmark or Sweden is measured,
  because spec 3.4 forbids the exchange's own spelling with a space. Also update the
  `grammar_tradingview.csv` row count in `tests/conformance/test_markets_reference_data.py` and
  `EXPECTED_TRADINGVIEW_GRAMMAR_ROWS` in the script when rows are added.
- Mapping notes: `DSEBD` is Dhaka (`XDHA`), not Chittagong; `UPCOM` is ISO segment `XHNX` of
  `HSTC`; ghana, kazakhstan and mongolia answered `404 page not found`.

## Live observations

None: no network was used (data endpoints forbidden; the uv fallback was not needed).

## README / CHANGELOG lines (for task 16)

- CHANGELOG: `- Source symbol grammars: \`crocodile.core.markets.grammar\` maps a listing (operating MIC + local symbol) to Yahoo chart (\`THYAO.IS\`), Google Finance (\`THYAO:IST\`) and TradingView (\`BIST:THYAO\`) spellings and back; unknown markets, suffixes and codes raise \`NoGrammarForMarket\` instead of guessing.`
- CHANGELOG: `- Packaged tables \`grammar_yahoo_chart.csv\` (55 rows, derived from the research matrix), \`grammar_google_finance.csv\` (3 rows) and \`grammar_tradingview.csv\` (13 rows, curated from committed scanner probes), each row with a sample symbol and dated evidence.`
- README (markets section): `Unsuffixed Yahoo symbols are shared by NASDAQ and NYSE; pass \`mic=\` to \`from_source\` to choose one.`

## Fix round 1

- BASE: `3b58ba20cf4e16c1f8624de9feec590e208d4685`.
- Finding (important, `tests/conformance/test_symbol_grammar.py`): `test_each_mic_has_exactly_one_primary_spelling` asserted `set(tradingview) == _TRADINGVIEW_MICS`, a second copy of the 13-MIC literal. Task 11 appends measured rows and owns only `test_the_tables_hold_the_documented_markets` for that literal, so this test would have failed on a new MIC. The reported line 603 does not exist in the file; the assertion is at line 138.
- Fix: the assertion now reads `set(tradingview) == {row.mic for row in load_grammar("tradingview").rows}`. The check that each MIC has exactly one primary (`set(tradingview.values()) == {1}`) is unchanged. `_TRADINGVIEW_MICS` remains in use by `test_the_tables_hold_the_documented_markets`, so the literal now appears only there.
- Verification: `tests/core/markets tests/conformance/test_symbol_grammar.py tests/conformance/test_markets_reference_data.py tests/conformance/test_markets_layering.py tests/conformance/test_calendar_cross_checks.py` gives 657 passed. `ruff check` on `tests/core/markets`, the grammar test file and `scripts/build_markets_reference.py` is clean, and `ruff format --check` on the test file is clean.
- mypy strict on the test file reports 6 `union-attr` errors (`SuffixRow | PrefixRow`). The same 6 errors occur at BASE, and none are on the changed line, so they are pre-existing and out of scope for this fix.
