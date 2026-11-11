import importlib

import pytest


def test_the_token_bucket_is_in_core():
    mod = importlib.import_module("crocodile.core.ratelimit")
    assert hasattr(mod, "TokenBucket")
    assert hasattr(mod, "TokenBucketLimiter")


@pytest.mark.parametrize("name", ["ProxyRotator", "ApiKeyPool"])
def test_evasion_machinery_is_not_in_core(name: str) -> None:
    mod = importlib.import_module("crocodile.core.ratelimit")
    assert not hasattr(mod, name), (
        f"{name} is evasion machinery and belongs in crocodile.contrib.evasion, "
        "so that 'legal by construction' is the product's default posture"
    )


def test_the_limiter_does_not_know_about_proxies_or_keys():
    """The coupling that put evasion in the core is the one to break here."""
    import inspect

    from crocodile.core.ratelimit import TokenBucket

    params = set(inspect.signature(TokenBucket.__init__).parameters)
    assert "proxy_rotator" not in params
    assert "api_key_pool" not in params

    leaked = [
        name
        for name in dir(TokenBucket)
        if any(word in name for word in ("proxy", "api_key", "key_"))
    ]
    assert not leaked, f"proxy/key delegation still on TokenBucket: {leaked}"


def test_the_shared_budget_registry_is_in_core() -> None:
    """Tasks 10 and 11 import budget() from core; the registry must not live in contrib."""
    mod = importlib.import_module("crocodile.core.ratelimit")
    assert hasattr(mod, "budget")
    assert hasattr(mod, "BudgetSpec")


def test_the_budget_module_imports_nothing_from_contrib() -> None:
    """budget.py is core: no contrib import, and no proxy or API-key machinery by name."""
    import ast
    from pathlib import Path

    path = Path(importlib.import_module("crocodile.core.ratelimit.budget").__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported: list[str] = []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.alias):
            names.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)

    assert not [m for m in imported if m.startswith("crocodile.contrib")]
    # MappingProxyType is the stdlib read-only mapping that DEFAULT_BUDGETS is specified as;
    # it is not evasion machinery, so it is excluded from the name check.
    lowered = [n.lower() for n in names if n != "MappingProxyType"]
    assert not [n for n in lowered if "proxy" in n or "api_key" in n]
