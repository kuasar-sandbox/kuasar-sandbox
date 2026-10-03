"""Explicit source-time release inputs; never a runtime capability registry."""
from pathlib import Path
import shutil

# Whole maintained user documents. Chinese counterparts are copied when present;
# the source documentation checker owns the translation policy.
GUIDES = {
    'platform': ('README', 'docs/quickstart', 'docs/download', 'docs/deployment',
                 'docs/kuasar-sandbox', 'docs/terminology', 'test/README',
                 'test/QUICKSTART', 'test/demo/DEMO', 'workbench/README'),
    'accelerator': ('README', 'docs/cache', 'docs/cache-redis', 'docs/store',
                    'docs/manifest', 'docs/file-artifacts', 'test/e2e/README'),
    'connector': ('README', 'docs/tapfd', 'docs/vswitch-operations'),
    'guest-runtime': ('README', 'docs/flatten', 'docs/sandbox-runtime',
                      'docs/vmlinux', 'test/e2e/README'),
    'sandboxer': ('README', 'docs/sandbox', 'docs/sandbox-init'),
    'orchestrator': ('README', 'docs/node', 'docs/node-build', 'docs/node-proxy',
                     'docs/node-resource', 'docs/node-journald', 'docs/telemetry'),
    'vmlinux': ('docs/vmlinux',),
}

PLATFORM_RUNTIME = (
    'test/e2e/e2e', 'test/e2e/lib/common.sh', 'test/e2e/lib/workspace.py',
    'test/e2e/lib/demo_wheels.py', 'test/demo/demo_common.sh',
    'test/demo/demo_e2b.sh', 'test/demo/demo_prep.sh', 'test/demo/prepared.py',
    'test/demo/requirements.txt', 'test/demo/requirements.lock',
    'workbench/workbench',
)
OWNER_LIBRARIES = {
    'platform': ('failure-diagnostics.sh', 'failure_diagnostics.py'),
    'accelerator': ('manifest_fixture.py', 'port_lease.sh'),
    'connector': ('notify_helpers.sh', 'stats_management.py'),
    'guest-runtime': ('assertions.py', 'common.sh', 'fixture.py', 'process.py'),
    'sandboxer': ('usage_oom_wrapper.py', 'resource_stats.py', 'readiness_helpers.sh',
                 'read_fault_proxy.py', 'usage_report_relay.py', 'usage_vsock_relay.py',
                 'usage_ch_relay.py', 'usage.py', 'tarstream.sh',
                 'usage_ch_wrapper.py', 'usage_vsock_wrapper.py'),
    'orchestrator': (
        'runner_lifecycle.py', 'workload.py', 'builder_client.sh',
        'resource_observation.sh', 'envd_exec.py', 'websocket_probe.py',
        'native_usage.sh', 'prepare_base_image.sh', 'execute_contract.sh',
        'builder_network.sh', 'builder_artifact.sh', 'build_fixture_units.sh',
        'builder_storage.sh', 'execute_template.sh', 'builder_ownership.sh',
        'mmds_service_guest.sh', 'builder_journal.sh', 'build_client.py',
        'journal_identity.py', 'telemetry.sh', 'execute_resource.sh', 'proxy.sh',
        'telemetry_backend_probe.py', 'envd_start.py', 'resource.sh',
        'execute_artifact.sh', 'placer_readiness.py', 'mmds_secret_guest.sh',
        'telemetry_probe.py', 'vmm_cgroup.sh', 'case_workspace.sh',
        'guest_service.sh', 'telemetry_stats.sh', 'proxy_case.sh',
        'execute_sandbox.sh', 'native_traffic.sh', 'proxy_guest.sh',
        'execute_state.sh', 'tarstream.sh', 'execute_client.sh',
        'execute_process.sh', 'execute_network.sh', 'execute_env.sh',
        'builder_proxy.sh', 'builder_env.sh', 'mmds_static_guest.sh',
        'builder_process.sh', 'cluster_env.sh', 'execute_instrument.sh',
        'cluster_process.sh', 'builder_config.sh', 'runtask_privilege.sh',
        'cluster_http.sh',
    ),
}


def regular_input(root: Path, path: Path):
    source = root / path
    if any((root / parent).is_symlink() for parent in (path, *path.parents)):
        raise ValueError(f'symbolic link in package input: {source}')
    if not source.is_file():
        raise ValueError(f'missing package input: {source}')
    return source


def copy_input(root: Path, path: Path, target: Path):
    source = regular_input(root, path)
    if target.exists():
        raise ValueError(f'package input collision: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
