"""Universal equity sources: a federated set of keyless aggregator and directory sources.

"Universal" is a chain alias for the sources of this package, never a source name on a
record: a record names the concrete source that produced it (spec section 3.10). Submodules are
not imported here; each source is imported by the module that needs it, so importing the
package costs nothing and pulls in no network client.
"""
