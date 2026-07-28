"""What has to be true before Phase 2 may begin.

Phase 1 pulled one core out of two forks. These are the assertions that say it
actually happened, rather than that it looks like it happened from a distance.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import subprocess

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "crocodile"


def _package_files() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_subtree_import_directory_is_gone() -> None:
    assert not (_REPO / "src" / "_incoming_stockodile").exists(), (
        "Task 1 imported Stockodile into a transient directory; Phase 1 dissolves it"
    )


def test_the_package_tree_is_anchored_where_this_test_thinks_it_is() -> None:
    """Guard the guards.

    Every scan below walks ``_SRC``. If that path stopped resolving — a rename,
    a move, running from another directory — the scans would find nothing and
    pass in silence, which is exactly how two AST gates went green while
    checking an empty tree earlier in this merge.
    """
    assert _SRC.is_dir(), f"package tree not found at {_SRC}"
    assert len(_package_files()) > 100, "package tree is implausibly small; check the anchor"


def test_the_package_being_imported_is_the_package_being_scanned() -> None:
    """The other half, and it is not hypothetical in a worktree.

    Half the gates in this suite read the tree off disk and half of them ``import crocodile``.
    Those are only the same subject when the interpreter resolves the package to *this*
    checkout, and an editable install does not guarantee it: this repository's venv carries
    a ``.pth`` naming one absolute source directory, so any interpreter started without
    ``pythonpath`` reads whichever tree that file points at. A worktree, a second clone, or a
    rebase in progress all make that a different tree — and a conformance suite that scans
    one checkout while importing another is green about nothing in particular.

    ``pyproject``'s ``pythonpath = ["src"]`` is what makes it come out right under pytest.
    This is the assertion that says so out loud, so the day someone removes that line the
    failure names the cause instead of appearing as an unrelated gate going strange.
    """
    import crocodile

    imported = pathlib.Path(crocodile.__file__ or "").resolve().parent
    assert imported == _SRC.resolve(), (
        f"scanning {_SRC.resolve()} but importing {imported}. These gates would be measuring "
        f"two different checkouts; check `pythonpath` in pyproject.toml and the editable "
        f"install's .pth."
    )


def test_no_source_file_still_imports_the_old_packages() -> None:
    """Nothing under `crocodile` may still *depend* on `crypcodile` / `stockodile`.

    The check is on imports, not on the words. Both names legitimately survive
    as text: CLI help, PyPI install hints, and — deliberately — the on-disk
    state paths ``~/.crypcodile/`` and ``STOCKODILE_HOME``, which were left
    alone so the rename does not orphan a live deployment's cache and sync
    state. Renaming those is a migration, not a rename, and it is not Phase 1's.
    """
    offenders: list[str] = []
    for path in _package_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                root = name.split(".", 1)[0]
                if root in {"crypcodile", "stockodile"}:
                    offenders.append(f"{path.relative_to(_REPO)}:{node.lineno} → {name}")

    assert not offenders, f"stale package imports: {offenders}"


def test_the_two_old_distributions_are_shims_and_nothing_else() -> None:
    """`src/crypcodile` and `src/stockodile` hold a deprecation notice, no code.

    A shim that still carried modules would keep the old import paths working by
    duplicating the tree, which is the opposite of a merge.
    """
    for legacy in ("crypcodile", "stockodile"):
        files = sorted(
            p.name for p in (_REPO / "src" / legacy).rglob("*.py") if "__pycache__" not in p.parts
        )
        assert files == ["__init__.py"], f"src/{legacy} still ships modules: {files}"


def test_the_deprecated_commands_say_they_are_deprecated() -> None:
    """A silent alias is not a deprecation.

    Both old commands still work, and both are removed in 0.4. Pointed straight
    at the merged CLI they would have run without a word, leaving the notice in
    a pyproject comment nobody reads.
    """
    for command, replacement in (("crypcodile", "crocodile"), ("stockodile", "crocodile-equity")):
        result = subprocess.run(
            [str(_REPO / ".venv" / "bin" / command), "--help"],
            cwd=_REPO,
            capture_output=True,
            text=True,
        )
        assert "deprecated" in result.stderr, f"{command} ran without a deprecation notice"
        assert "0.4" in result.stderr, f"{command} does not say when it goes away"
        assert replacement in result.stderr, f"{command} does not name its replacement"


def test_the_public_api_is_no_longer_empty() -> None:
    import crocodile

    assert getattr(crocodile, "__all__", None), (
        "both source repos shipped an empty __init__; the merge exists partly to fix that"
    )


def test_every_public_name_actually_resolves() -> None:
    """`__all__` that lies is worse than `__all__` that is empty."""
    import crocodile

    missing = [name for name in crocodile.__all__ if not hasattr(crocodile, name)]
    assert not missing, f"__all__ names that do not exist: {missing}"


def test_no_exception_class_escapes_the_root() -> None:
    """Carried forward from Task 3: the `__all__` walk has one hole.

    Task 3's root check iterates `errors.__all__`, so a class defined in the
    module and forgotten from `__all__` is invisible to it. Walk the module's
    own namespace instead — that hole is exactly how an exception ends up
    outside the hierarchy the root promises to cover.
    """
    from crocodile.core import errors
    from crocodile.core.errors import CrocodileError

    strays = [
        name
        for name, obj in vars(errors).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseException)
        and obj.__module__ == errors.__name__
        and not issubclass(obj, CrocodileError)
    ]
    assert not strays, f"exception classes outside CrocodileError: {strays}"


# ---------------------------------------------------------------------------
# The promise the merge actually made
# ---------------------------------------------------------------------------

# Names that were public before the merge and are deliberately gone. Each needs
# a reason, and the reason has to be better than "we did not need it".
_DELIBERATELY_DROPPED = {
    # A test stub that lived inside the equity fork's *duplicate* of the Base L2
    # connector. The duplicate was deleted as fork residue — an on-chain
    # connector inside an equities library — and the surviving crypto connector
    # has the real CoinbaseSmartWalletDetector. Dropping a stub that shadowed a
    # real implementation is the point of the deduplication, not a loss.
    "DummySmartWalletDetector",
    # The two equity record structs the canonical union absorbed rather than
    # copied, so they are the only names the union merge removed from the tree.
    # Neither is a capability: `test_record_parity.py::_STRUCT_RENAMES` names the
    # struct each one resolves to and proves, field by field against the frozen
    # pre-merge snapshot, that nothing they declared has nowhere to go.
    #
    # `Bar` was field-for-field identical to equity's own `OHLCV`, which is a
    # duplicate within one fork rather than across two; `OHLCV` survives because
    # it says what the record contains. `trade_count` arrives as `num_trades`.
    "Bar",
    # `OptionQuote` and crypto's `OptionsChain` describe one option contract in
    # two dialects, and the crypto spelling wins because it distinguishes a price
    # from a size (`bid`/`ask`/`last` → `bid_px`/`ask_px`/`last_price`). Its one
    # field with no crypto counterpart, `volume`, moved onto `OptionsChain`.
    #
    # Both tags stay declared on `Channel` and both are keys of
    # `CHANNEL_SUCCESSORS`, so `channel=bar/` and `channel=option_quote/`
    # partitions still decode — the struct left, the on-disk vocabulary did not.
    "OptionQuote",
    #
    # ------------------------------------------------------------------
    # Phase 2: the six hand-written surface stacks
    # ------------------------------------------------------------------
    # Everything below was a *handler* in one of the six deleted stacks, and
    # every one of them is now generated. Read the wire names instead of this
    # list: `tests/conformance/test_phase2_surface_parity.py` asserts, against a
    # fixture frozen while all six were intact, that no command, route, tool or
    # parameter a caller could reach stopped answering. That gate is the one
    # that measures this deletion; these names are its implementation detail,
    # and losing an implementation detail while the contract holds is the
    # difference between a refactor and the seven silent losses above.
    #
    # 27 Typer command bodies. One per crypto CLI command, each a hand-written
    # function that parsed its own options, opened its own Catalog and printed
    # its own table. `surfaces/cli.py` synthesises all of them from
    # `Capability.params` with no per-capability branch anywhere in it.
    "basis_cmd",
    "census_cmd",
    "chaos_score_cmd",
    "collect_market_cmd",
    "data_coverage_cmd",
    "funding_apr_cmd",
    "funding_predict_cmd",
    "gas_vol_cmd",
    "iv_surface_cmd",
    "label_transfers_cmd",
    "lending_stress_cmd",
    "liquidity_depth_cmd",
    "list_exchanges_cmd",
    "markets_cmd",
    "mev_sandwich_cmd",
    "ofi_cmd",
    "open_interest_cmd",
    "peg_deviation_cmd",
    "resolve_symbols_cmd",
    "risk_reversal_cmd",
    "sequencer_latency_cmd",
    "slippage_cmd",
    "smart_money_cmd",
    "term_structure_cmd",
    "universe_cmd",
    "vol_skew_cmd",
    "whale_alerts_cmd",
    # 33 MCP tool handlers. The same list a third time, as `elif tool_name ==`
    # branches with per-branch argument unpacking and per-branch error strings.
    # `surfaces/mcp.py::call_tool` is the whole of it now, and `surfaces/stdio.py`
    # dispatches every tool through that one call.
    "handle_calculate_ofi",
    "handle_catalog_stats",
    "handle_catalog_summary",
    "handle_data_coverage",
    "handle_detect_mev_sandwiches",
    "handle_estimate_slippage",
    "handle_get_chaos_score",
    "handle_get_funding_prediction",
    "handle_get_indicators",
    "handle_get_iv_surface",
    "handle_get_lending_stress",
    "handle_get_liquidity_depth",
    "handle_get_open_interest",
    "handle_get_peg_deviation",
    "handle_get_perp_basis",
    "handle_get_risk_reversal",
    "handle_get_sequencer_latency",
    "handle_get_spot_future_basis",
    "handle_get_spot_perp_basis",
    "handle_get_term_structure",
    "handle_get_vol_skew",
    "handle_inventory_snapshot",
    "handle_label_transfers",
    "handle_list_all_exchanges",
    "handle_list_data_channels",
    "handle_list_dates",
    "handle_list_exchanges_on_disk",
    "handle_list_registered_exchanges",
    "handle_list_symbols",
    "handle_resolve_symbols",
    "handle_search_symbols",
    "handle_smart_money_summary",
    "handle_track_whale_alerts",
    # 11 FastAPI route bodies, the same list a fourth time. `surfaces/rest.py`
    # generates one route per capability from the same struct MCP publishes as
    # its inputSchema, which is what makes the two surfaces unable to disagree
    # about what a caller may send.
    "catalog_list_channels",
    "catalog_list_dates",
    "catalog_list_exchanges",
    "catalog_list_symbols",
    "catalog_search_symbols",
    "exchanges",
    "get_depth",
    "query_lake",
    "simulate_price_impact",
    "custom_swagger_ui_html",
    "sse_events",
    # 6 pydantic bodies for the POST routes. Every capability is a GET with query
    # parameters now — one rule, and a POST body was only ever how a hand-written
    # route accepted a list before `build_params` learned to split one.
    "GasVolPayload",
    "LabelTransfersPayload",
    "MevSandwichPayload",
    "PriceImpactPayload",
    "QueryPayload",
    "SmartMoneyPayload",
    # The x402 on-chain verification path. It gated exactly one route —
    # `GET /api/v1/market-data`, the Base DEX price — by fetching a receipt over
    # RPC with a five-attempt failover and matching a USDC Transfer log against a
    # hardcoded contract, recipient and amount. That capability is now
    # `onchain-price`, declared and served without a paywall, so the verifier has
    # no room behind its door. The ledger it wrote to survives, because
    # `simulate-payment` and `admin/payments` administer it; see
    # `surfaces/payments.py`, which says the same thing at length.
    "get_market_data",
    "get_w3",
    "switch_rpc_failover",
    "is_connection_or_rate_limit_error",
    "get_transaction_receipt_with_failover",
    "get_transaction_with_failover",
    "lifespan",
    "PersistentDict",
    "get_payments_file",
    "load_payments_db",
    "save_payments_db",
    # The demo dashboard. `get_dashboard_html` read
    # `api_portal/public/index.html` off disk and fell back to a 65-line literal
    # whose own comment says it exists "satisfying E2E test string expectations".
    # The Node portal it served went with the paid route it demonstrated;
    # `surfaces/server.py::root_dashboard` is a landing page that links to the
    # generated schema and therefore cannot go stale.
    "get_dashboard_html",
    # The interactive CLI. A wizard that prompted for a symbol, then a channel,
    # then a time range, then confirmed — reachable only from a tty, and the
    # reason the crypto CLI could not be driven by a script without
    # `is_interactive_stdin` returning False. It has no projection because a
    # prompt is not a parameter: the same request has to be expressible on a
    # command line, in a query string and in a JSON schema.
    "prompt_symbol",
    "prompt_time_range_helper",
    "prompt_with_autocomplete",
    "select_collect_params_interactively",
    "select_symbols_interactively",
    "get_console",
    # The CLI's own rendering and normalisation helpers, which the projection
    # answers once for every capability instead of once per command:
    # `make_sparkline`, `get_record_value` and `format_record_value` rendered the
    # live `collect` dashboard; `expand_csv_options` and `unique_preserve` split
    # repeated flags, which `build_params` now does for every sequence field; and
    # `run_dashboard`/`MonitoringSink` were the dashboard itself.
    "make_sparkline",
    "get_record_value",
    "format_record_value",
    "expand_csv_options",
    "unique_preserve",
    "run_dashboard",
    "MonitoringSink",
    # `resolve_data_dir` walked `test_data`, the repo root and
    # `~/Crypcodile/test_data` looking for a lake with channels in it, and
    # answered from whichever it found when the one on the command line was
    # empty. `tests/conformance/test_data_dir_resolution.py` is the gate that
    # exists because of exactly this, and `Settings` is the one resolver now.
    "resolve_data_dir",
    # `normalize_user_symbol` and `resolve_input_symbols` guessed a venue for a
    # bare symbol — `BTC` became `binance-usdm:BTCUSDT` under a derivative
    # channel and `binance-spot:BTCUSDT` under a spot one, against an empty lake.
    # `resolve-symbols` resolves against the lake's own inventory and raises when
    # there is no match, which is the same question answered from evidence. The
    # per-venue normalisation `collect` and `backfill` used is a real behavioural
    # difference and `CollectParams`' docstring already records it as pending a
    # lift into `crypto.instruments`.
    "normalize_user_symbol",
    "resolve_input_symbols",
    # Three options-chain path helpers that existed to find the newest snapshot
    # date by globbing the lake directory. `iv-surface`, `term-structure`,
    # `vol-skew` and `risk-reversal` all take `at_ns` and read through
    # `CapabilityContext.query`, so the glob has no caller.
    "get_available_option_expiries",
    "get_available_option_snapshots",
    "get_available_option_underlyings",
    "get_latest_options_chain_date_glob",
}


def _merged_public_names() -> set[str]:
    names: set[str] = set()
    for path in _package_files():
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if not node.name.startswith("_"):
                    names.add(node.name)
    return names


def test_no_capability_disappeared_in_the_merge() -> None:
    """Every public name either fork had is still reachable, or is listed with a reason.

    This is the assertion the merge needed from the start and did not have.
    Seven capabilities were promoted away and lost — the read-only SQL guard,
    the Catalog lifecycle, the non-bucketed lake reader, six resamplers,
    `OrderBookSync`, the equity `from_row`, the equity `resample_ohlcv` — each
    on the unmeasured assumption that the crypto copy was the superset. None of
    the existing gates saw it, because they all ask whether an implementation
    exists rather than whether one stopped existing. Nothing raised; queries
    just came back empty.

    The inventory beside this file is the two forks' public surface at the last
    commit where both trees were intact. Deleting a name from it to make this
    pass is the one thing that defeats it.

    The snapshot has been shrunk once, deliberately, and the distinction that
    licensed it is the one this gate is about. It catches a name that stopped
    existing *while nobody decided it should* — a promotion that assumed the
    other fork's copy was a superset, a projector that emitted a smaller table.
    It was never a promise that the product may not retire a feature. The commit
    that deleted the PyQt6 desktop layer took eight names off the ``crypto``
    list: ``flowmap``, ``gas_tracker`` and ``run_flowmap_gui`` (the launchers),
    and ``FlowmapWindow``, ``GasTrackerWidget``, ``dict_to_flowmap_objects``,
    ``compute_hist_target_bw`` and ``compute_hist_vis_rows`` (the window and its
    helpers). ``_DELIBERATELY_DROPPED`` was the other available shape and would
    have kept the file frozen; it was not used because every existing entry
    there is a name whose *behaviour* survives somewhere else under a different
    spelling, and these have no survivor to point at — the argument for them is
    that a headless data engine should not ship a Qt dependency, which belongs
    in a commit message rather than in a one-line exemption. FlowMap remains a
    published product; it is the launcher that left this repository.
    """
    inventory = json.loads((pathlib.Path(__file__).parent / "premerge_public_api.json").read_text())
    merged = _merged_public_names()

    lost = {
        fork: sorted(set(names) - merged - _DELIBERATELY_DROPPED)
        for fork, names in inventory.items()
        if not fork.startswith("_")
    }
    assert not any(lost.values()), (
        "public names that existed before the merge and exist nowhere now: "
        f"{ {k: v for k, v in lost.items() if v} }. Either restore them or add them to "
        "_DELIBERATELY_DROPPED with a reason."
    )


def test_the_dropped_list_is_not_quietly_hoarding_names() -> None:
    """A name that came back must leave the exemption list.

    Otherwise the list grows into a place where losses go to be forgotten.
    """
    merged = _merged_public_names()
    resurrected = sorted(_DELIBERATELY_DROPPED & merged)
    assert not resurrected, f"these exist again and no longer need an exemption: {resurrected}"


# ---------------------------------------------------------------------------
# Inherited debt: capped, not hidden
# ---------------------------------------------------------------------------

# Ruff findings in the two legacy surfaces on the day Phase 1 closed. They are
# inherited — both forks carried them under their old names, where nothing was
# ever pointed at them. The number exists so the debt can only shrink: lower it
# when you fix something, and never raise it. `core` and `contrib` are held to
# zero separately below, and that is the line new code is written on.
#
# 476 → 444: Track A rewrote the import block of 44 crypto modules, and ruff's
# isort rule was already unhappy with 32 of them.
# 444 → 431: the same for 23 equity modules on the other half of Track A.
# 431 → 430: the tree had been sitting a finding below its own cap. A budget with
# slack in it is a budget that lets the next finding in for free, which is the
# whole thing a ratchet is for.
# 430 → 429: the two forked `analytics/slippage.py` modules merged into one under
# `core`, which is held to zero, so their inherited findings left the legacy tree
# with them.
# 429 → 428: the two data-dir resolvers in the crypto REST server became one, and
# its import block was reformatted on the way past.
# 428 → 240: Phase 2 deleted the six hand-written surface stacks — 15 899 lines,
# and 188 of the findings were theirs. Note the ratchet did *not* fall by the
# whole of what left, because `crypto/legacy/mcp_server.py` did not entirely go —
# the two Base pool readers moved to `crypto/exchanges/base_onchain/price.py`, and
# their findings moved with them rather than being fixed. That is deliberate: this
# commit is a deletion, and reformatting code on its way past is how a deletion
# stops being reviewable.
# 240 → 222: no fix, a measurement. The line above used to add that "what is left
# is 238 in `crypto` and 2 in `equity`" — and the merge then took 18 more out of
# `crypto` without anybody lowering the cap, so the tree sat at 222 under a budget
# of 240 with room for 18 new findings to land green. That is the exact condition
# the 431 → 430 step was taken to remove, and it had reappeared behind a sentence
# that described the split instead of asserting it. The composition is no longer
# written here for the same reason: it is `_ruff("src/crocodile/crypto")` and
# `_ruff("src/crocodile/equity")`, and a prose copy of a number only ever goes out
# of date in the direction that loosens the gate.
# 222 → 202: no fix, a deletion. `crypto/gui/` — the PyQt6 FlowMap window and the
# gas-tracker widget — left the tree with the rest of the desktop layer, and its 20
# inherited findings left with it. Lowered in the same commit rather than a later
# one, because a budget sitting 20 above the tree is 20 new findings that land green,
# which is the condition the 431 → 430 and 240 → 222 steps were both taken to remove.
_LEGACY_RUFF_BUDGET = 202


def _ruff(*paths: str) -> list[str]:
    result = subprocess.run(
        [".venv/bin/python", "-m", "ruff", "check", "--output-format=concise", *paths],
        cwd=_REPO,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if ": " in line]


# Every package that is not inherited debt. `_LEGACY_RUFF_BUDGET` covers `crypto` and
# `equity`; between them these two lists must cover `src/crocodile` entirely, or a new
# top-level package lands outside every lint gate — which is how a prohibited constant
# once moved into the one place nothing looked.
_CLEAN_PACKAGES = (
    "src/crocodile/core",
    "src/crocodile/contrib",
    "src/crocodile/capabilities",
    "src/crocodile/surfaces",
    "src/crocodile/__init__.py",
    "src/crocodile/_deprecated_cli.py",
    "src/crypcodile",
    "src/stockodile",
)


def test_the_merged_core_is_lint_clean() -> None:
    """Everything Phase 1 actually wrote holds to zero."""
    findings = _ruff(*_CLEAN_PACKAGES)
    assert not findings, f"{len(findings)} findings in core/contrib:\n" + "\n".join(findings[:20])


def test_every_package_is_covered_by_one_of_the_two_lint_gates() -> None:
    """A package on neither list is lint-unchecked, and nothing would say so.

    This walked ``src/crocodile`` and therefore could not see the thing it was written to
    catch. ``src/`` holds three top-level packages, not one: the ``crypcodile`` and
    ``stockodile`` deprecation shims sit beside ``crocodile``, on neither list, and each
    carried a finding that no gate in the tree had ever counted — ``ruff check src`` read
    224 while both gates together accounted for 222. A coverage test scoped one directory
    below the thing it claims to cover is the same vacuous shape as a gate over an empty
    registry: green, and about nothing.

    So the subject is ``src`` itself, and it recurses far enough to find a package that is
    neither top-level nor inside one of the listed roots.
    """
    gated = {
        pathlib.PurePath(p)
        for p in (*_CLEAN_PACKAGES, "src/crocodile/crypto", "src/crocodile/equity")
    }
    src = _SRC.parent
    ungated = sorted(
        str(rel)
        for module in src.rglob("*.py")
        if "__pycache__" not in module.parts
        for rel in [module.relative_to(src.parent)]
        if not any(root == rel or root in pathlib.PurePath(rel).parents for root in gated)
    )
    assert not ungated, (
        f"{len(ungated)} file(s) under no ruff gate: {ungated[:10]}. Add each to "
        f"_CLEAN_PACKAGES (held to zero) or to the legacy ratchet — a file on neither is "
        f"lint-unchecked and nothing else in the tree would report it. Files, not "
        f"packages: `src/crocodile/_deprecated_cli.py` is in no subpackage and was "
        f"invisible to the version of this test that walked directories."
    )


def test_the_legacy_surfaces_do_not_get_worse() -> None:
    """The inherited debt is a ratchet, not a licence."""
    findings = _ruff("src/crocodile/crypto", "src/crocodile/equity")
    assert len(findings) <= _LEGACY_RUFF_BUDGET, (
        f"legacy lint debt grew to {len(findings)}, budget is {_LEGACY_RUFF_BUDGET}. "
        "Phase 2 lowers this number; nothing raises it."
    )
