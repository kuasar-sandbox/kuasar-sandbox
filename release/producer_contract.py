"""Release dependencies on each side of the sandbox-init ownership change."""
from pathlib import Path

import go_dependency_alignment as alignment

LEGACY_INPUTS = {
    "runtime": ("accelerator", "sandboxer"),
    "sandboxer": ("accelerator", "connector"),
    "orchestrator": ("accelerator", "connector", "sandboxer"),
}
RUNTIME_ABI_INPUTS = {
    "runtime": ("accelerator",),
    "sandboxer": ("accelerator", "connector", "runtime"),
    "orchestrator": ("accelerator", "connector", "sandboxer", "runtime"),
}


def checkout_dependencies(unit, source: Path):
    """Read the product tree already bound by producer admission."""
    if unit == "orchestrator":
        manifest = source / "go.mod"
        if manifest.is_symlink() or not manifest.is_file():
            raise ValueError("missing regular orchestrator go.mod")
        modern = alignment.policy.PREFIX + "guest-runtime" in alignment.policy.managed_requires(manifest.read_text())
    else:
        commands = source / "cmd"
        marker = commands / ("flatten-ctl" if unit == "runtime" else "sandbox-ctl")
        if commands.is_symlink() or marker.is_symlink() or not marker.is_dir():
            raise ValueError(f"{unit}: unexpected command inventory")
        init = commands / "sandbox-init"
        if init.is_symlink() or (init.exists() and
                (not init.is_dir() or (init / "main.go").is_symlink() or not (init / "main.go").is_file())):
            raise ValueError(f"{unit}: invalid sandbox-init command")
        modern = init.is_dir() == (unit == "runtime")
    return (RUNTIME_ABI_INPUTS if modern else LEGACY_INPUTS)[unit]
