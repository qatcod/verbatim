"""Connectors — bring external communication sources into Verbatim.

Each connector is responsible for turning a foreign format (Slack export ZIP,
GitHub PR API response, Anthropic Console audit log, etc.) into the same flat
text transcript representation that the extractor consumes. The extractor and
state layer don't know about source format; that's the connector's contract.
"""
