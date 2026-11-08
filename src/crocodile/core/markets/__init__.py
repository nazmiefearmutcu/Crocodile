"""Market-neutral reference data and registries for Crocodile.

This package holds what every market needs and nothing that belongs to one
source or one provider: ISO 10383 market identifiers, ISO 4217 currencies,
the exchange allow-list with weekend and close facts, sanctions defaults,
quote-unit parsing and the exact-arithmetic helpers that turn quoted prices
into major-unit values.

Layering rule: ``crocodile.core.markets`` imports nothing from
``crocodile.crypto``, ``crocodile.equity``, ``crocodile.capabilities``,
``crocodile.surfaces`` or ``crocodile.contrib``. Those layers depend on this
package, never the reverse. ``tests/conformance/test_markets_layering.py``
enforces the rule.

The package imports no submodule eagerly, so ``import crocodile.core.markets``
stays cheap. Import the names you need from their own modules.
"""

__all__: list[str] = []
