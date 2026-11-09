"""Shared export errors and metadata limit, independent of report presentation."""

# Project engineering bound for metadata only; original videos are streamed by verification.
MAX_MANIFEST_BYTES = 1024 * 1024

# Project engineering bounds for the verifier, NOT sourced standards: they stop a hostile or corrupt
# archive from exhausting memory or disk, and sit far above what an approved report package holds
# (a handful of videos plus a few hundred small documents). Revisit when real package sizes are known.
MAX_PACKAGE_MEMBERS = 2_000
MAX_PACKAGE_BYTES = 8 * 1024**3


class PackageRefused(ValueError):
    """Readable export refusal without local paths or underlying DB/storage diagnostics."""


class VerificationRefused(ValueError):
    """Readable integrity or archive portability refusal."""
