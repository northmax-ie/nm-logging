"""Concrete persistence sinks.

A sink owns the append, framing, and (for audit, M5) durability mechanics behind
the Sink protocol in interfaces.py. Nothing above the sink interface knows about
files, lines, paths, or encodings (§22); a sink is where that knowledge lives.
"""
