"""Universal equity sources: a federated set of keyless aggregator and directory sources.

"Universal" is a chain alias for the sources of this package, never a source name on a
record: a record names the concrete source that produced it (spec section 3.10). Submodules are
not imported here; each source is imported by the module that needs it, so importing the
package costs nothing and pulls in no network client.
"""

# The one import here is the parent package, not a submodule. base.py needs
# crocodile.equity.providers.base, whose package __init__ imports the factory, which imports
# yahoo_chart, which imports base and http. Loading the providers package before any submodule
# body runs lets that cycle resolve in every entry order (a cold import of universal.http failed).
import crocodile.equity.providers  # noqa: F401
