"""Local authentication (Security Phase). See docs/PRIVACY_SECURITY.md §6.

No cloud identity provider, no multi-user login -- FERChronos has exactly
one local owner. This package holds password/recovery-key hashing (Step
1), and will grow session issuance/verification and the enforcement
middleware in later, separately-approved steps.
"""
