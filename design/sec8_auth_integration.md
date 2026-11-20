## 8. Authentication & Authorization Integration

**Document status:** Draft v0.1
**Depends on:** sec2_architecture.md (§2.7 auth placeholder), sec6_provenance.md (actor field), Bridge sec4_auth.md (auth design)
**Feeds into:** Bridge sec4_auth.md (§4.9.1 Hippo integration point), Implementation

---

### 8.1 Design Philosophy

Hippo's auth story has two distinct modes that coexist by design:

1. **SDK mode (no auth):** The `MosaicClient` accepts an `actor` string parameter with no
   validation. There is no Bridge, no credential check, and no middleware. This is correct for
   local single-user deployments where auth friction is unwanted.

2. **REST + Bridge mode (auth enforced):** When Hippo is deployed behind Bridge, Bridge owns
   all credential validation. Hippo's transport layer reads validated identity context from
   Bridge-injected headers and trusts them unconditionally. Hippo never holds signing keys,
   user stores, or session tables.

The `AuthMiddleware` ABC defined in `hippo/rest/auth.py` (stubbed in v0.1) is the seam between
these two modes. Replacing the stub with `BridgeAuthMiddleware` is sufficient to enable
full auth — no restructuring of the SDK or REST app is required.

---

### 8.2 `AuthMiddleware` ABC

The ABC defines the contract that any real auth implementation must fulfil:

```python
# hippo/rest/auth.py

from abc import ABC, abstractmethod
from fastapi import Request


class AuthMiddleware(ABC):

    @abstractmethod
    def authenticate(self, request: Request) -> str:
        """
        Extract and return the authenticated actor identity from the request.

        Returns:
            str: The actor identity string to be used in all provenance events
                 for this request.

        Raises:
            HTTPException(401): If credentials are missing or invalid.
        """
        ...

    @abstractmethod
    def authorize(self, actor: str, operation: str, entity_type: str) -> bool:
        """
        Determine whether the authenticated actor may perform the requested
        operation on the given entity type.

        Args:
            actor: The identity string returned by authenticate().
            operation: One of "read", "write", "delete", "schema_admin",
                       "availability_change", "provenance_read".
            entity_type: The entity type name, or "*" for cross-type operations.

        Returns:
            bool: True if the operation is permitted; False to raise 403.
        """
        ...


class PassThroughAuthMiddleware(AuthMiddleware):
    """
    No-op stub used in v0.1 standalone deployments.
    Returns "anonymous" for all requests and permits all operations.
    """

    def authenticate(self, request: Request) -> str:
        return request.headers.get("X-DataHelix-Actor", "anonymous")

    def authorize(self, actor: str, operation: str, entity_type: str) -> bool:
        return True
```

The stub (`PassThroughAuthMiddleware`) is registered as the default in the FastAPI app. The
`BridgeAuthMiddleware` replaces it when `bridge.enabled: true` is set in `mosaic.yaml`.

---

### 8.3 `BridgeAuthMiddleware` — Bridge-Aware Implementation

When Hippo is deployed behind Bridge, all incoming requests have already been authenticated.
Bridge strips the original `Authorization` header and injects two verified headers:

| Header | Content | Example |
|---|---|---|
| `X-DataHelix-Actor` | Authenticated actor identity from the JWT `datahelix:actor` claim | `alice@uni.edu` |
| `X-DataHelix-Roles` | Comma-separated RBAC roles from the JWT `datahelix:roles` claim | `analyst,viewer` |

`BridgeAuthMiddleware` reads these headers and maps them to Hippo's auth model:

```python
# hippo/rest/auth_bridge.py

from fastapi import Request, HTTPException
from hippo.rest.auth import AuthMiddleware

# Maps Bridge roles to permitted Hippo operations.
_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin":        {"read", "write", "delete", "schema_admin",
                     "availability_change", "provenance_read"},
    "project_lead": {"read", "write", "availability_change", "provenance_read"},
    "analyst":      {"read", "write", "provenance_read"},
    "viewer":       {"read", "provenance_read"},
    "service":      {"read", "write"},
}


class BridgeAuthMiddleware(AuthMiddleware):
    """
    Auth middleware for Hippo deployments running behind Bridge.
    Trusts X-DataHelix-Actor and X-DataHelix-Roles headers injected by Bridge.
    Never validates JWT signatures — that is Bridge's responsibility.
    """

    def authenticate(self, request: Request) -> str:
        actor = request.headers.get("X-DataHelix-Actor")
        if not actor:
            raise HTTPException(
                status_code=401,
                detail="Missing X-DataHelix-Actor header. "
                       "Requests must be routed through Bridge.",
            )
        return actor

    def authorize(self, actor: str, operation: str, entity_type: str) -> bool:
        # roles header is set by Bridge; absence means no roles granted
        roles_header = ""
        # roles are stored on the request context by the middleware layer;
        # implementation passes them in via a thread-local or request state
        # (see §8.4 for request context wiring)
        permitted = False
        for role in roles_header.split(","):
            role = role.strip()
            if operation in _ROLE_PERMISSIONS.get(role, set()):
                permitted = True
                break
        return permitted
```

> **Note:** The roles header is accessed via the FastAPI request state object (populated
> by middleware before `authorize` is called — see §8.4). The simplified sketch above shows
> the lookup logic; the wiring is an implementation detail.

#### 8.3.1 Header Trust Boundary

`X-DataHelix-Actor` and `X-DataHelix-Roles` are only trusted when they arrive from Bridge's internal
network:

- Bridge strips and rewrites these headers on all proxied requests
- If Hippo receives a request directly (bypassing Bridge) that contains `X-DataHelix-Actor`,
  it must be rejected with `403 Forbidden` — components must not be exposed on the public
  network when Bridge auth is expected
- The `mosaic.yaml` `bridge.trust_proxy` configuration key (CIDR list) controls which source
  IPs are allowed to set these headers; requests from outside this range that carry
  `X-DataHelix-Actor` are rejected

#### 8.3.2 Backwards Compatibility: `X-DataHelix-Actor` Header

The v0.1 `X-DataHelix-Actor` header (caller-supplied actor) is superseded when Bridge auth is
active. `BridgeAuthMiddleware.authenticate()` ignores `X-DataHelix-Actor` entirely and reads only
`X-DataHelix-Actor`. This prevents actor spoofing — a caller behind Bridge cannot override the
authenticated identity by setting `X-DataHelix-Actor`.

In `PassThroughAuthMiddleware` (standalone mode), `X-DataHelix-Actor` continues to be read as
before, for backwards compatibility.

---

### 8.4 Request Context Wiring

The FastAPI app registers auth middleware as a startup-time dependency:

```yaml
# mosaic.yaml (relevant keys)
bridge:
  enabled: true                        # Activates BridgeAuthMiddleware
  trust_proxy: ["10.0.0.0/8"]         # CIDRs allowed to set X-DataHelix-* headers
```

At request time the middleware pipeline runs:

```
Incoming request
      │
      ▼
┌─────────────────────────────────┐
│  BridgeTrustFilter              │
│  Verify source IP is in         │
│  trust_proxy CIDR list          │
│  → 403 if X-DataHelix-* set from     │
│    untrusted source             │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  BridgeAuthMiddleware           │
│  authenticate() → actor string  │
│  Store actor + roles on         │
│  request.state                  │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│  Route handler                  │
│  Reads actor from request.state │
│  Passes actor to SDK call       │
│  SDK writes actor to provenance │
└─────────────────────────────────┘
```

The actor string stored on `request.state.actor` is the single value passed into every
`MosaicClient` call made during that request. There is no ambient global state — the actor is
threaded explicitly through the call chain.

---

### 8.5 Actor Propagation to Provenance

Every Hippo write operation accepts `actor` as an explicit parameter:

```python
# Provenance event produced by a Bridge-authenticated write
{
  "id": "evt-uuid",
  "event_type": "EntityCreated",
  "entity_id": "ent-uuid",
  "entity_type": "Sample",
  "actor": "alice@uni.edu",          # ← value from X-DataHelix-Actor (Bridge-verified)
  "timestamp": "2026-09-14T10:32:11Z",
  "schema_version": "2.1",
  "context": {
    "request_id": "req-uuid",
    "bridge_project": "genomics-lab-a"  # Optional: project context injected by Bridge
  },
  "payload": { "initial_state": { ... } }
}
```

Actor format conventions:

| Actor format | Source | Example |
|---|---|---|
| `user@domain` | Human user authenticated via OIDC | `alice@uni.edu` |
| `service:<name>` | Service account (Client Credentials flow) | `service:cappella-runner` |
| `apikey:<label>` | API key with a human-readable label | `apikey:ingest-script` |
| `anonymous` | `PassThroughAuthMiddleware` (standalone mode) | `anonymous` |

The `actor` value in provenance is **always** the Bridge-verified identity when Bridge is
active. Route handlers must not override it with values from query params or request bodies.

---

### 8.6 Bridge Auth Events and the Audit Trail

Hippo's provenance records data mutation events only. Authentication lifecycle events (login,
logout, token refresh, API key creation/revocation) are **not** written to Hippo's provenance
log — they belong to Bridge's own audit log.

The full audit trail for a data-mutating action spans two logs:

```
Bridge audit log                 Hippo provenance log
────────────────                 ────────────────────
2026-09-14T10:32:10Z             2026-09-14T10:32:11Z
  alice@uni.edu authenticated      EntityCreated: Sample/ent-uuid
  via OIDC (login event)           actor: alice@uni.edu
                                   context.request_id: req-uuid
```

The `request_id` in the Hippo provenance context links a data mutation back to the Bridge
request that originated it. `admin` users can correlate Bridge's auth log and Hippo's
provenance log via this ID when auditing a specific mutation.

#### 8.6.1 What Bridge logs (Bridge audit log)

| Event | Logged by |
|---|---|
| User login / logout | Bridge |
| Token issuance / refresh / revocation | Bridge |
| API key creation / revocation / rotation | Bridge |
| Authorization denials (403 responses) | Bridge |
| Schema admin operations (schema upload) | Bridge (enforced at gateway) |

#### 8.6.2 What Hippo logs (Hippo provenance)

| Event | Logged by |
|---|---|
| Entity created / updated | Hippo |
| Availability change (`is_available`) | Hippo |
| Entity supersession | Hippo |
| Relationship created / removed | Hippo |
| Reference data installed | Hippo |
| Schema migration applied | Hippo |

The actor on every Hippo provenance event is the Bridge-authenticated identity, providing
end-to-end traceability from authenticated session through to the data change.

---

### 8.7 Project Scoping Enforcement

Bridge enforces project scoping at the gateway level (see Bridge sec4 §4.3.3). Hippo does
not enforce project scoping independently — it trusts that requests arriving through Bridge
have already been filtered to the actor's permitted projects.

When Bridge proxies a request it may inject an additional header:

| Header | Content |
|---|---|
| `X-DataHelix-Projects` | Comma-separated list of project IDs the actor may access |

Route handlers for entity queries pass this list as a filter to the SDK. Entities whose
`project` field does not appear in the list are excluded from query results.

**Note:** If the schema does not declare a `project` field on the entity type, project
scoping is disabled for that type and all entities are returned for authenticated users with
appropriate role.

---

### 8.8 Configuration Reference

```yaml
# mosaic.yaml — auth section (new in Phase 3)

bridge:
  enabled: false                     # Default: standalone mode (PassThroughAuthMiddleware)
  trust_proxy:                       # Source CIDRs allowed to inject X-DataHelix-* headers
    - "127.0.0.1/32"                 # loopback (local dev)
    - "10.0.0.0/8"                   # internal network (production)
  reject_direct_datahelix_headers: true   # Reject X-DataHelix-* from untrusted sources (default: true)
```

Deployment tiers:

| Tier | `bridge.enabled` | Auth middleware | Actor source |
|---|---|---|---|
| **Local dev (standalone)** | `false` | `PassThroughAuthMiddleware` | `X-DataHelix-Actor` header or `"anonymous"` |
| **Team server (Bridge-enabled)** | `true` | `BridgeAuthMiddleware` | `X-DataHelix-Actor` from Bridge |
| **SDK direct** | N/A | None | `actor=` parameter on SDK calls |

---

### 8.9 Open Questions

| Question | Priority | Notes |
|---|---|---|
| Should Hippo validate JWT signatures independently (offline mode)? | Low | Bridge injects actor via header — component JWT validation is redundant. Document the offline-capable pattern if needed. |
| `X-DataHelix-Projects` enforcement in SDK — should SDK expose a project filter API? | Medium | Currently enforced at REST route level; SDK has no native project concept. |
| Auth event fan-out — should Bridge push auth events into Hippo's provenance via a webhook? | Medium | Current design keeps logs separate; cross-log correlation via `request_id` is sufficient for v1.0. Revisit if compliance requirements demand a single audit stream. |
