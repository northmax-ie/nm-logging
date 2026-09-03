"""Synthetic sensitive-looking material for the test suite.

Everything in this file is SYNTHETIC and exists only to exercise the library's
secret-material and hygiene guarantees. None of it is, or ever was, a real
credential. The values are obviously non-random, recognisable strings chosen so
that a hygiene test can assert they appear in no emitted record, no fallback
line, no exception text, no traceback, and no ``caplog`` output.

They will nonetheless trip secret scanners, so these exact values (not this file
path) are allowlisted in ``.gitleaks.toml`` and each carries an inline
``pragma: allowlist secret`` marker for detect-secrets. The file itself stays
scanned for any other secret. Do not replace the value allowlist with a path
allowlist, which would disable scanning for a whole file.

Nothing here is imported by the M1 record-model tests; the constants exist from
the start so the scanner allowlist references material that is actually present,
and so the M2+ hygiene tests have a single home for their markers.
"""

# A recognisable marker with no secret shape. Used to prove that a value routed
# through a field never survives into any emitted or fallback text.
SECRET_MARKER = "NMLOG_SYNTHETIC_MARKER_do_not_leak_1a2b3c"  # pragma: allowlist secret

# A synthetic encrypted envelope. Shaped like an ENC[...] envelope so the
# §11 field guard's ``ENC[`` / ``]`` rejection can be exercised; the payload is
# plain text, not real ciphertext.
SYNTHETIC_ENC_ENVELOPE = "ENC[v1:aes256gcm:gen2:c3ludGhldGljLW5vdC1yZWFs]"  # pragma: allowlist secret

# A fake bearer token, secret-shaped, to prove such a value never reaches a
# record or a failure channel even on exception paths.
SYNTHETIC_BEARER_TOKEN = "Bearer nmlog-synthetic-not-a-real-token-000000"  # pragma: allowlist secret
