"""Hermes Portal — multi-user gateway and audit spine for Hermes Agent.

The portal is deliberately a separate service from the Hermes core. It owns
accounts, roles, tenant tokens, the append-only audit store, and the admin
console. Tenant Hermes containers keep running upstream code; a small audit
plugin ships their events here.
"""

__version__ = "0.1.0"
