[English](templates.md) | [简体中文](templates_zh.md)

# Build and reuse templates

This guide takes an application developer from an input image to a reusable template. It requires a standalone node entry that passed [deployment acceptance](deployment.md#production-start) and the SDK selected by that release. Cluster clients additionally follow the [Group/route contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/cluster.md); the example here uses a standalone node.

## Choose the state to publish

| Target | Use | What an instance does |
| --- | --- | --- |
| Image | Deliver an application filesystem and launch configuration | Initialize and start the application |
| Sandbox E | Reuse prepared disk state | Cold-start a new run without restoring process memory |
| Snapshot S | Reuse initialized processes and memory | Restore the capture in a compatible execution environment |

Select explicit targets with the [Build target recipe](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md#client-workflow). The example below uses the current SDK's auto selection and checks the result; a startCmd alone is not a specification of the executed phases.

<a id="first-application"></a>
## Your first template and application

After the Demo, use a running Node with the existing [Node connection and API-key
contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-proxy.md#sdk-configuration).
Reuse its `E2B_API_KEY`, `E2B_API_URL`, `E2B_DOMAIN`, data ingress/DNS and trusted TLS
CA settings. The Demo stops its services at completion: its temporary credentials
and container localhost addresses do not configure an independent host client.
For exploration, keep the documented Demo `--pause` session alive, or provision
your Node through [Deployment](deployment.md). Never copy Demo credentials into a
production deployment.

This recipe targets the repository-pinned **e2b==2.25.1** and current
[Build contract](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md).
Use the SDK pin and guide shipped with your selected aggregate if different;
a current-source example does not certify an older Release. Set `APP_IMAGE` to
your own digest-pinned, architecture-matching registry image containing Python 3,
a shell and a writable `/tmp`. Configure registry credentials through Node's
existing credential mechanism. First check [image and kernel adaptation](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/flatten-cli.md#decide-whether-your-workload-needs-adaptation).
The 2 CPU/6 GiB build budget follows the Demo, not a sizing recommendation.

The build command prepares files; the start command runs the application; the
readiness command checks its HTTP response before snapshot publication. A fixed
sleep alone does not prove your application is ready. Nonempty start/ready commands
select the auto memory target. No register-only configuration headers are passed
to `Template.build`, because this SDK forwards them to trigger too.

```python
import json, os, urllib.request
from e2b import Sandbox, Template

api = os.environ["E2B_API_URL"].rstrip("/")
key = os.environ["E2B_API_KEY"]
image = os.environ["APP_IMAGE"]  # registry/repository@sha256:...
start = "python3 -m http.server 8080 --directory /tmp/my-app"
ready = "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=2)\""
tpl = (Template().from_image(image)
       .run_cmd("mkdir -p /tmp/my-app && printf 'my application\\n' > /tmp/my-app/index.html")
       .set_start_cmd(start, ready))
info = Template.build(
    tpl, name="my-first-app", cpu_count=2, memory_mb=6144,
    on_build_logs=lambda entry: print(entry.message, flush=True),
)
request = urllib.request.Request(
    f"{api}/templates/{info.template_id}/builds/{info.build_id}/status",
    headers={"X-API-KEY": key})
with urllib.request.urlopen(request, timeout=30) as response:
    status = json.load(response)
assert status["buildID"] == info.build_id and status["status"] == "ready"
assert status["profile"] == "e2b" and status["kind"] == "snp"
assert status["target"] is None
canonical_id = status["templateID"]
assert canonical_id.startswith("e2b-snp-")
print("Keep canonical template ID:", canonical_id)
sandbox = Sandbox.create(canonical_id, timeout=300)
try:
    assert sandbox.commands.run("cat /tmp/my-app/index.html").exit_code == 0
    sandbox.files.write("/tmp/checkpoint-proof", "preserve me")
    sid = sandbox.sandbox_id
    sandbox.pause()
    sandbox = Sandbox.connect(sid)
    assert sandbox.files.read("/tmp/checkpoint-proof") == "preserve me"
    assert "my application" in sandbox.commands.run(
        "python3 -c \"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/').read().decode())\"").stdout
finally:
    sandbox.kill()
```

Expected results: exact-build ready status with `kind=snp`, a canonical
`e2b-snp-...` ID, successful command/file access, and matching state after resume.
Keep the callback output: SDK 2.25.1 logs the registration and build IDs before
trigger/wait, so they remain available if the build raises instead of returning
`info`. Retain both registration/build handles for diagnostics and the canonical ID for
future creates; the SDK's returned registration handle is not the published ID.
For explicit Image, cold Sandbox E or memory Snapshot S, use the
[separate registration/trigger recipe](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md#client-workflow).

For build failure, inspect that build's status/logs and image pull, phase capacity
and ready-command failure before retrying. Missing COPY storage is a separate
unsupported boundary; this example uses no COPY. A failed pause is not a retained
snapshot. `kill` cleans the instance, not the published template's remote data.
Use [Build cancel/delete](https://github.com/kuasar-sandbox/orchestrator/blob/main/docs/node-build.md#cancel-and-delete-a-build-record)
for the saved registration handle and [retention](operations.md#retention-runbook)
for artifacts. Do not delete parent data to tidy up a failed experiment.
## Reuse, update and clean up

1. Retain the exact Build's `buildID`, registration handle and canonical `templateID` from ready status. Use the handle for diagnosis/Build cleanup and the canonical ID for later creates.
2. Create multiple instances from that canonical ID and verify distinct identity and write isolation. Publishing another template does not update existing instances.
3. Build and qualify a new template for an application change. Check startup, commands, files, application readiness and the needed recovery path before switching new creates to its ID.
4. Keep the old template, full parent closure, keys and runtime/kernel dependencies for rollback. [Upgrades](upgrading.md) explains memory-state compatibility limits.
5. Use Build Cancel/DELETE for build records and the [retention procedure](operations.md#retention-runbook) for artifacts. Build-record cleanup, instance deletion and remote-artifact retirement are different operations.

## Input adaptation and failure handling

Use an architecture-matching, digest-pinned image. Check its shell, user, file permissions, start command, required kernel features and network-client tools using the [flatten CLI guide](https://github.com/kuasar-sandbox/guest-runtime/blob/main/docs/flatten-cli.md). COPY also needs configured upload storage; a recipe without COPY does not validate it.

On readiness failure, retain the exact build's logs and IDs and distinguish image retrieval, admission, build command, application start, ready-check and publication failures. Hand the canonical ID to users only after the exact build is ready and a real create succeeds. A fixed sleep does not replace an application ready check.
