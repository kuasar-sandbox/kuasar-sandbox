[English](security.md) | [简体中文](security_zh.md)

# Platform security boundaries and responsibilities

This guide defines deployment trust boundaries and protection responsibilities. Root [SECURITY](../SECURITY.md) owns vulnerability reporting and supported-version policy. Implementing components own exact token, encryption, network and MMDS protocols.

## Trust boundaries and deployment checks

| Boundary | Relationship the deployment must establish |
| --- | --- |
| Untrusted Guest and trusted Host | Use the selected KVM/VMM/Guest combination and isolation interfaces; keep host control sockets, storage keys and management networking outside arbitrary Guest access |
| Platform user and API | Configure public TLS, credentials and tenant ownership; temporary Demo credentials are not production identity |
| Business access and remote execution | Issue and forward capabilities for their intended purpose; do not give the Guest an API root credential as an application-port token |
| Node and cluster services | Authenticate node-link as specified and protect Router-to-node networking; public HTTPS does not secure other hops automatically |
| Content-sharing domain | Scope sharing through keys, named locations and backend permissions; equal bytes do not authorize cross-tenant access |
| Guest egress and management services | vSwitch establishes trusted port identity, the external gateway applies selected policy, and MMDS exposes Conductor-authorized routes and registered local services |

Current cluster Router-to-node APIEndpoint/DataEndpoint use internal plaintext HTTP/CONNECT. Isolate that network and terminate public TLS at Router or its load balancer. Node-link mTLS is configured independently. Standalone direct TLS listeners cannot simply be reused as cluster plaintext endpoints; see [deployment](deployment.md).

## Tenant security

- Platform identity credentials and content-protection keys have separate purposes. APISecret authenticates platform identity and control-plane operations; ManifestKey protects the content-key domain.
- Control-plane access, ordinary data-plane access and remote execution use credentials with different scopes. Remote-execution tokens bind a particular subject, purpose and conditions.
- Content-protection keys remain in trusted host data paths and are not injected into the guest.
- Local and remote artifacts can be encrypted and verified according to deployment policy. Key protection, backup and rotation remain operator responsibilities.
- Content-key and security-domain configuration determine deduplication and sharing scope. Identical content does not itself justify sharing across tenants.
- Stable Sandbox ID, credential binding and trusted network-identity boundaries remain valid after pause, resume and migration.
- Network isolation, node admission and fault domains combine with data encryption to establish tenant security; no single mechanism substitutes for all of them.

Credential updates affect only later business records that are created with copied credentials. Credentials already persisted in existing sandbox records retain their original binding; updating a key-distribution table does not automatically rebind those records.

## Secret plugins

Secret plugins let trusted host components obtain key bytes from a deployment's
local secret broker. Set `KUASAR_SECRET_PLUGINS` before starting each consuming
service, then use `@<plugin>/<key>` in the supported configuration fields:

```bash
export KUASAR_SECRET_PLUGINS='kms::/run/secret-broker1.sock:cache_ttl=30s:cache_size=4k enc::/run/secret-broker2.sock:cache_ttl=30s'
export MANIFEST_KEY='@kms/tenant-a/manifest-v1'
export NODE_CONFIG_ENCRYPTION_KEY='@enc/node-key-v2:@enc/node-key-v1'
```

| Field | Ordinary value | Plugin result |
| --- | --- | --- |
| ManifestKey, including `MANIFEST_KEY` and `manifest.key` | 64-character hex key | Raw 32-byte key |
| Each EncryptionKey ring entry | 64-character hex key | Raw 32-byte key |
| Configured TLS `key` | Private-key file path | PEM private-key bytes |
| Configured static S3 `access_key` / `secret_key` | Literal credential | Credential bytes |

Quote references in YAML. Each plugin declaration names an absolute Unix socket;
multiple declarations are separated by whitespace. Plugin keys keep their text
after the first `/` and cannot contain colons or whitespace. A leading `@` is
reserved in the listed fields: use `./@filename` or an absolute path for a TLS
filename starting with `@`, and a plugin reference for such an S3 literal value.

The host service must be able to reach the configured socket. For containerized
services, mount the broker's socket directory at that path and provide the
environment variable inside the container. Node tasks receive the trusted node
plugin configuration; independently launched storage or proxy services need
their own environment. Do not expose the broker socket to arbitrary Guests.

Each plugin has an independent LRU cache, defaulting to 30 seconds and 4096
entries. A zero TTL or capacity disables caching. Cache expiry affects later
resolutions; it does not replace keys already held by storage objects, TLS
certificates or static S3 clients. Use stable, versioned references. Keep old
EncryptionKey ring entries while their records need decryption; the first
entry encrypts new records.

Existing application-injected credential providers retain precedence. Secret
plugins resolve bytes only: they do not perform remote signing, unwrap keys or
refresh STS credentials. APISecret and ServiceSecret keep their existing
configuration semantics. Plugin errors do not fall back to another credential
source. See the accelerator [Secret plugin contract](https://github.com/kuasar-sandbox/accelerator/blob/main/docs/secret-plugins.md)
for the HTTP-over-UDS protocol, parser rules and SDK interface.

References keep actual key bytes out of environment values and configuration
fields. Resolved bytes still exist in the consuming process's memory; ordinary
hex values keep their existing behavior. Artifact encryption formats and the
relationship between ciphertext and its actual key remain unchanged.

## MMDS route security and deployment boundaries

Conductor configuration owns `mmds.listen`, route admission policy and the local-service registry. Supported sandbox/Build requests supply concrete MMDS routes; Conductor validates and projects those routes under that policy. Tenant routes additionally require `mmds.routes.enabled` and cannot be enabled while MMDS is disabled.

When `mmds.enabled=false`, envd runs without its own authentication, so `proxy.auth=enforce` is required as the sole data-plane gate. When MMDS is enabled, Proxy provides the MMDS v2 flow supporting envd initialization with the sandbox's bound `EnvdAccessToken`. New E2B identities receive a random token by default, but supported explicit overrides and identity-preserving resume/migration retain their credential binding; initialization is not automatic token rotation. See the [Proxy MMDS contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy.md#mmds) for the supported authentication postures.

Start and register the independent Proxy before opening new sandbox admission; Create requires its route barrier. Verify the data listener and routing before serving workloads. When MMDS is enabled, also verify that Proxy master uses the Conductor-configured MMDS listener in the intended network namespace and that vSwitch management traffic reaches it. Local services use explicitly configured absolute Unix-socket URIs; do not expose their host sockets to guests.

The [Proxy MMDS contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy.md#mmds) owns request identity, route matching, secret handling, header filtering and forwarding restrictions; [Build](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md) owns build-scoped registration and terminal cleanup. Public API TLS/authentication, local socket permissions, guest-isolated networking and service credentials must be configured together. Avoid a second service registry in proxy YAML.

<a id="key-incidents"></a>
## Key incidents

For suspected key exposure, stop new publication with the affected credentials,
restrict access through the deployment's existing controls, and preserve incident
and dependency evidence. Coordinate application, key and storage owners. Existing
node rows keep their bound credentials; removing an allowlist/provider entry is
not retroactive revocation. Do not assume in-place Manifest key rotation or remote
ciphertext revocation. Create and validate replacement artifacts/credentials through
supported workflows, update consumers deliberately, and retain old keys securely
while old snapshots remain required. If a key is lost, recover it from the approved
backup; without it the encrypted artifacts cannot be declared recoverable. Never
purge old data as a key-incident repair step.

## Acceptance and retention

Verify rejection of missing credentials, wrong tenants, wrong Group/route key, unauthorized ports and disallowed MMDS routes, while authorized real work succeeds. Evidence can include key identifiers, never secret bytes. Data/key retirement requires the [complete retained-root check](operations.md#retention-runbook).
