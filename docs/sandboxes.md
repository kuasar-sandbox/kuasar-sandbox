[English](sandboxes.md) | [简体中文](sandboxes_zh.md)

# Use and manage sandboxes

This guide is for applications using an installed platform. It covers creating an
instance, commands/files, application access, pause/resume, explicit cold start,
reset and cleanup. [Concepts](concepts.md) explains the objects; Orchestrator's
[managed lifecycle](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/sandbox-lifecycle.md)
owns exact API fields, state transitions, errors and retention behavior.

## Prepare the connection

Obtain the selected release's SDK, `E2B_API_URL`, `E2B_API_KEY`, `E2B_DOMAIN`, trusted
TLS CA settings and a usable canonical `TEMPLATE_ID` from the platform operator.
The examples below use the repository-pinned **e2b==2.25.1**. Follow your selected
release's pin when it differs. A completed Demo normally stops its temporary node;
use its documented interactive session or a separately deployed service.

For a cluster, also retain `KUASAR_GROUP` and the caller-chosen route key used for
that instance. They must accompany later control and data requests. A Group's port
constraints must allow the intended service. The [cluster client contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md)
owns provider setup, routing and header requirements. A sandbox ID alone is not
the complete cluster routing context.

## Create, execute, save state and clean up

Set the connection variables above. If `KUASAR_GROUP` is set, the example generates
one route key and reuses it throughout. Persist the printed group/route/Sandbox
identity in an application that outlives this client process; do not regenerate
the route key when reconnecting to that instance.

```python
if not __debug__:
    raise RuntimeError("Run this recipe without Python optimization (-O/-OO or PYTHONOPTIMIZE).")

import os, uuid
from e2b import Sandbox

headers = {}
if os.environ.get("KUASAR_GROUP"):
    headers = {
        "X-Kuasar-Sandbox-Group": os.environ["KUASAR_GROUP"],
        "X-Kuasar-Route-Key": str(uuid.uuid4()),
    }
options = dict(api_url=os.environ["E2B_API_URL"],
               api_key=os.environ["E2B_API_KEY"],
               domain=os.environ["E2B_DOMAIN"], headers=headers)
sandbox = Sandbox.create(os.environ["TEMPLATE_ID"], timeout=300, **options)
sid = sandbox.sandbox_id
print("Retain routing identity:", headers, sid)
try:
    result = sandbox.commands.run("printf sandbox-ready")
    assert result.exit_code == 0 and result.stdout == "sandbox-ready"
    sandbox.files.write("/tmp/session-proof", "session-state")
    assert sandbox.files.read("/tmp/session-proof") == "session-state"
    sandbox.pause()
    sandbox = Sandbox.connect(sid, **options)
    assert sandbox.sandbox_id == sid
    assert sandbox.files.read("/tmp/session-proof") == "session-state"
    assert sandbox.commands.run("test -f /tmp/session-proof").exit_code == 0
finally:
    sandbox.kill()
```

Success means the actual command/file assertions, pause, reconnect and cleanup
complete. Create/Connect can accept a launch before Runtime or envd is ready;
the first data operation can wait on the starting route. This file assertion
checks retained filesystem state; the [template example](templates.md#first-application) also checks service availability. To prove memory restoration, check a known application-memory value written before pause and never persisted to disk. A fixed file or HTTP response alone cannot distinguish resumed memory from an application restart.

The same template can produce many independent instances. Each receives its own
identity and writes; deleting one instance does not delete the shared template.

## Inspect state and choose an explicit policy

The following helper targets an **existing instance you own**. Set `SANDBOX_ID`;
for a cluster, set both `KUASAR_GROUP` and the instance's saved `KUASAR_ROUTE_KEY`.
It uses the same trusted CA environment as other Python HTTPS clients and does
not disable certificate verification. HTTP errors are raised to the caller.

```python
import json, os, urllib.parse, urllib.request

api_url = os.environ["E2B_API_URL"].rstrip("/")
sid = os.environ["SANDBOX_ID"]
route_headers = {}
if os.environ.get("KUASAR_GROUP"):
    route_headers = {
        "X-Kuasar-Sandbox-Group": os.environ["KUASAR_GROUP"],
        "X-Kuasar-Route-Key": os.environ["KUASAR_ROUTE_KEY"],
    }
instance_path = "/sandboxes/" + urllib.parse.quote(sid, safe="")

def control(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        api_url + path, data=data, method=method,
        headers={"X-API-KEY": os.environ["E2B_API_KEY"],
                 "Content-Type": "application/json", **route_headers})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
        return json.loads(raw) if raw else None

state = control("GET", instance_path)
print(state["sandboxID"], state["state"])
```

Listing depends on the entry point:

- **Standalone Conductor:** `GET /v2/sandboxes` is tenant-scoped and supports the
  lifecycle contract's `state` filter, `limit` and `nextToken` parameters, with
  the next page token returned in `x-next-token`. Default listing omits some
  transient and terminal states; use explicit filters for diagnosis.
- **Cluster Router:** the same collection path returns the Group's current
  Registry projection for `ready`, `pausing` and `paused` routes. It does not
  forward the node's `state`/`limit`/`nextToken` parameters or provide that
  pagination contract. These route states are not the full node lifecycle view.
  Use the ID-based Get helper above with the saved Group/route key to inspect a
  known instance. See the [cluster control contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md#router-control-plane).

Absence from a default list is not proof that the instance has been deleted.
Resource, traffic and usage stats are read-only observations, not application
readiness checks; their availability and historical coverage are defined in
[observability](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/observability.md).

### Pause and resume choices

| Intent | Operation | Application consequence |
| --- | --- | --- |
| Continue the captured process later | Pause with default/true memory, then ordinary Connect | S-backed state uses memory restore when compatible |
| Keep disks and restart processes later | Pause with `memory: false` | Produces E-only paused state; later automatic Connect cold-starts it |
| Explicitly restart from retained disks | Connect a paused source with `memory: false` | Does not resume the old process memory |
| Require saved memory | Connect a paused instance with `memory: true` | Requires a usable S source; it does not silently downgrade to cold start |

These memory choices apply when restoring a **paused** instance. Connect on a **running** instance is idempotent; `memory: false` does not restart it. To save disks and cold-start an existing running instance, Pause first:

```python
control("POST", instance_path + "/pause", {"memory": False})
control("POST", instance_path + "/connect", {"memory": False, "timeout": 300})
```

Then verify a real command/file operation and application initialization. Do not
pass snapshot-only merge/drop options with `memory: false`. Keep the complete
source dependencies until the selected lifecycle operation releases ownership.
Cross-node recovery also requires [restore eligibility](upgrading.md#restore-eligibility).

### Timeout, idle policy and reset

Update the lifetime of an existing instance using its supported timeout operation:

```python
control("POST", instance_path + "/timeout", {"timeout": 300})
```

TTL, `autoPauseMemory`, explicit Pause and `idleTimeout` are separate policies.
Idle observation comes from trusted Proxy activity; it is not a statement that
arbitrary Guest background work has ended. Use the lifecycle contract's
`ifIdle`/`ifIdleSince` operation for conditional pause rather than assuming that a
client-side idle check and a later unconditional Pause are atomic.

For a **dead** instance whose disks remain usable, Reset converts it to an E-only
paused source. It does not launch a VM. Use Reset followed by Connect when the
application can restart from its disk state:

```python
control("POST", instance_path + "/reset")
control("POST", instance_path + "/connect", {"memory": False, "timeout": 300})
```

Other active states can conflict, and invalid/unavailable current disks can make
Reset fail while the instance remains dead. `autoReset` is a Create-time choice;
its failures remain visible for diagnosis. Neither manual nor automatic Reset
recovers uncaptured memory. Dead data remains only until Delete or the configured `dead_ttl` deadline; queries and failed retries do not renew it. Consult state, retention deadline and recorded errors before deciding
whether to retry, retain evidence or delete the instance.

## Access an application port

First run a server in the Guest, or use the [template](templates.md) example that
starts an HTTP service on port 8080. The public data hostname must have correct
DNS/TLS, and any Group port constraint must permit 8080. With the helper above:

```python
connection = control("POST", instance_path + "/connect", {})
token = connection["forwardAccessToken"]
request = urllib.request.Request(
    f"https://8080-{sid}.{os.environ['E2B_DOMAIN']}/",
    headers={"X-Access-Token": token, **route_headers})
with urllib.request.urlopen(request, timeout=60) as response:
    print(response.read().decode())
```

This uses the authenticated standard-HTTPS data entry. Use the deployment's
documented endpoint when it has a different public port or ingress convention.
Do not put the API key in the Guest or a browser URL. `get_host(port)` gives a
hostname; it does not inject cluster headers or scoped tokens into a browser.
Native remote exec has its own ExecAccessToken flow in the
[Proxy contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy.md).

## Finish and handle failures

Delete an instance when its retained state is no longer needed:

```python
control("DELETE", instance_path)
```

Deletion transfers cleanup responsibility to the owner. A successful response
does not mean every asynchronous directory/row finalizer has already completed.
Do not delete the instance's parent artifacts manually to force cleanup.

On control failure, inspect ownership, requested state, admission and source
availability. On control success with data failure, inspect Proxy registration,
Group/route headers, token scope, DNS/TLS, target port and application readiness.
A failed Pause is not evidence of a committed snapshot. Preserve the existing
source and first failure; [operations](operations.md) gives the recovery and
retention sequence.
