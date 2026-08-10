from __future__ import annotations

import unittest
from pathlib import Path

import working_set_report as report


FLAGS = {
    "A": (True, True),
    "B": (False, True),
    "C": (True, False),
    "D": (False, False),
}

REQUIRED_METRICS = (
    "b_memory_logical_bytes",
    "b_memory_allocated_bytes",
    "w_memory_logical_bytes",
    "w_memory_allocated_bytes",
    "w_disk_logical_bytes",
    "w_disk_allocated_bytes",
    "w_memory_resident_bytes",
    "w_memory_resident_ratio",
    "snapshot_pause_ms",
    "snapshot_dump_ms",
    "snapshot_wall_ms",
    "publish_wall_ms",
    "restore_ack_ms",
    "app_ready_ms",
    "first_request_ms",
    "uffd_faults",
    "uffd_pages_copied",
    "uffd_pages_zeroed",
    "lazy_load_ratio",
    "cache_origin_requests",
)

LOCAL_REQUIRED_METRICS = (
    "b_memory_logical_bytes",
    "b_memory_allocated_bytes",
    "w_memory_logical_bytes",
    "w_memory_allocated_bytes",
    "w_disk_logical_bytes",
    "w_disk_allocated_bytes",
    "w_memory_resident_bytes",
    "w_memory_resident_ratio",
    "snapshot_pause_ms",
    "snapshot_dump_ms",
    "snapshot_wall_ms",
    "restore_ack_ms",
    "app_ready_ms",
    "first_request_ms",
    "first_request_mib_per_s",
    "uffd_faults",
    "uffd_pages_copied",
    "uffd_pages_zeroed",
    "lazy_load_ratio",
    "uffd_source_read_calls",
    "uffd_source_read_bytes",
    "uffd_source_read_ms",
    "uffd_source_read_avg_us",
    "uffd_source_read_mib_per_s",
    "cache_origin_requests",
)


def complete_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for group, (drop_caches, merge_ref) in FLAGS.items():
        prefetch_modes = ("off", "memory") if group == "D" else ("off",)
        for prefetch in prefetch_modes:
            row: dict[str, object] = {name: 1 for name in REQUIRED_METRICS}
            row.update(
                {
                    "group": group,
                    "iteration": 1,
                    "drop_caches": drop_caches,
                    "merge_ref": merge_ref,
                    "prefetch": prefetch,
                    "prefetch_result": "completed" if prefetch == "memory" else "disabled",
                    "prefetch_started_ms": 4.0 if prefetch == "memory" else None,
                    "prefetch_duration_ms": 5.0 if prefetch == "memory" else None,
                    "cache_origin_bytes": None,
                    "disk_reads": {
                        name: {
                            "role": role,
                            "bytes": 4096,
                            "p50_us": 2.0,
                            "p99_us": 3.0,
                        }
                        for name, role in report.EXPECTED_DISK_ROLES.items()
                    },
                }
            )
            rows.append(row)
    return rows


def complete_local_crypto_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for cache_state in report.LOCAL_CRYPTO_CACHE_STATES:
        for policy in report.LOCAL_CRYPTO_POLICIES:
            for iteration in (1, 2):
                base = 100 + iteration + (10 if policy == "auto" else 0)
                row: dict[str, object] = {name: base for name in LOCAL_REQUIRED_METRICS}
                row.update(
                    {
                        "active_diff_format": "existing plaintext",
                        "cache_state": cache_state,
                        "crypto_local": policy,
                        "drop_caches": False,
                        "first_request_mib_per_s": 900 - base,
                        "group": "D",
                        "iteration": iteration,
                        "merge_ref": False,
                        "prefetch": "off",
                        "restore_source": "local tarstream",
                        "uffd_source_read_mib_per_s": 1900 - base,
                        "disk_reads": {
                            name: {
                                "role": role,
                                "bytes": 4096,
                                "p50_us": 2.0,
                                "p99_us": 3.0,
                            }
                            for name, role in report.EXPECTED_DISK_ROLES.items()
                        },
                    }
                )
                rows.append(row)
    return rows


class WorkingSetReportTest(unittest.TestCase):
    def test_nearest_rank_percentile(self) -> None:
        self.assertEqual(report.percentile([1, 2, 3, 4], 50), 2)
        self.assertEqual(report.percentile([1, 2, 3, 4], 95), 4)
        self.assertIsNone(report.percentile([], 99))

    def test_complete_matrix_renders_prefetch_and_missing_bytes(self) -> None:
        rows = complete_rows()
        report.validate(rows, allow_partial=False)
        rendered = report.render({"inputs": {"iterations": 1}}, rows, Path("samples.jsonl"))
        self.assertIn("| D/memory | false | false | memory | completed | 1 |", rendered)
        self.assertIn("cache/store origin transferred bytes", rendered)
        self.assertIn("N/A/N/A/N/A", rendered)
        self.assertIn("| A/off | blk4 | dataset.top |", rendered)

    def test_local_crypto_matrix_renders_paired_latency_and_throughput(self) -> None:
        rows = complete_rows()
        local_rows = complete_local_crypto_rows()
        report.validate_local_crypto(local_rows)
        rendered = report.render(
            {"inputs": {"iterations": 2}},
            rows,
            Path("samples.jsonl"),
            local_rows,
            Path("local-crypto-samples.jsonl"),
        )
        self.assertIn("## Local tarstream crypto overhead", rendered)
        self.assertIn("| cold/auto | auto | cold | false | false | off | existing plaintext | 2 |", rendered)
        self.assertIn("UFFD source ReadAt throughput", rendered)
        self.assertIn("### Paired auto - off change", rendered)
        self.assertIn("Local crypto samples: `local-crypto-samples.jsonl`", rendered)

    def test_rejects_missing_scenario(self) -> None:
        with self.assertRaisesRegex(SystemExit, "incomplete matrix"):
            report.validate(complete_rows()[:-1], allow_partial=False)

    def test_rejects_duplicate_sample(self) -> None:
        rows = complete_rows()
        rows.append(dict(rows[0]))
        with self.assertRaisesRegex(SystemExit, "duplicate sample"):
            report.validate(rows, allow_partial=False)

    def test_rejects_wrong_policy_mapping(self) -> None:
        rows = complete_rows()
        rows[0]["merge_ref"] = False
        with self.assertRaisesRegex(SystemExit, "want True/True"):
            report.validate(rows, allow_partial=False)

    def test_rejects_wrong_disk_backend_role(self) -> None:
        rows = complete_rows()
        rows[0]["disk_reads"]["blk4"]["role"] = "dataset.base"
        with self.assertRaisesRegex(SystemExit, "invalid disk role for blk4"):
            report.validate(rows, allow_partial=False)

    def test_rejects_incomplete_local_crypto_pair(self) -> None:
        with self.assertRaisesRegex(SystemExit, "local crypto scenario"):
            report.validate_local_crypto(complete_local_crypto_rows()[:-1])

    def test_rejects_local_crypto_diff_encryption(self) -> None:
        rows = complete_local_crypto_rows()
        rows[0]["active_diff_format"] = "encrypted"
        with self.assertRaisesRegex(SystemExit, "mixed active DIFF encryption"):
            report.validate_local_crypto(rows)


if __name__ == "__main__":
    unittest.main()
