#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM_SOURCE_ROOT="$(cd "${PLATFORM_SOURCE_ROOT:-$ROOT}" && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"

fetch_components() {
  [ "$#" -eq 2 ] || release_fail "usage: aggregate-release.sh fetch <release-version> <output-dir>"
  local version="$1" output="$2"
  assert_safe_output "$output"
  mkdir -p "$output/components" "$output/sources" "$output/updates"
  resolve_selection "$PLATFORM_SOURCE_ROOT" "$version" "$output/selection.tsv"
  local previous
  previous="$(previous_release "$PLATFORM_SOURCE_ROOT" "$version")"
  : > "$output/previous-selection.tsv"
  if [ -n "$previous" ]; then
    resolve_selection "$PLATFORM_SOURCE_ROOT" "$previous" "$output/previous-selection.tsv"
  fi

  local unit tag previous_tag repository archive release_state expected_prerelease unit_dir
  while IFS=$'\t' read -r unit tag; do
    previous_tag="$(awk -F '\t' -v unit="$unit" '$1 == unit {print $2}' \
      "$output/previous-selection.tsv")"
    repository="$(component_repository "$unit")"
    archive="$(component_archive "$unit" "$tag")"
    unit_dir="$output/components/$unit"
    mkdir -p "$unit_dir"
    release_state="$(github_api "repos/$repository/releases/tags/$tag")"
    expected_prerelease=false
    is_preview "$tag" && expected_prerelease=true
    jq -e \
      --arg tag "$tag" \
      --arg archive "$archive" \
      --argjson prerelease "$expected_prerelease" '
        .tag_name == $tag
        and .draft == false
        and .prerelease == $prerelease
        and (.assets | length == 2)
        and ([.assets[].name] | sort == (["SHA256SUMS", $archive] | sort))
        and ([.assets[].digest] | all(type == "string" and test("^sha256:[0-9a-f]{64}$")))
      ' <<< "$release_state" >/dev/null \
      || release_fail "$repository $tag does not satisfy the two-asset release contract"

    local name asset
    for name in "$archive" SHA256SUMS; do
      asset="$(jq -ce --arg name "$name" '.assets[] | select(.name == $name)' <<< "$release_state")"
      github_download_asset "$repository" "$(jq -er '.id' <<< "$asset")" "$unit_dir/$name"
      verify_github_asset "$unit_dir/$name" "$asset"
    done
    validate_component_download "$unit" "$tag" "$unit_dir"
    fetch_component_source "$repository" "$tag" "$output/sources/$unit"
    write_component_updates "$unit" "$repository" "$previous_tag" "$tag" \
      "$output/updates/$unit.md"
  done < "$output/selection.tsv"
}

fetch_component_source() {
  local repository="$1" tag="$2" output="$3"
  [ ! -e "$output" ] || release_fail "component source output already exists: $output"
  local archive listing roots
  archive="$(mktemp)"
  listing="$(mktemp)"
  curl --fail --show-error --silent --location \
    --retry 4 --retry-all-errors --connect-timeout 10 --max-time 300 \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer ${GH_TOKEN:?GH_TOKEN is required}" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "${GITHUB_API_URL:-https://api.github.com}/repos/$repository/tarball/$tag" \
    > "$archive"
  tar -tzf "$archive" > "$listing"
  awk '
    /^\// { exit 1 }
    { path=$0; if (path ~ /(^|\/)\.\.($|\/)/) exit 1 }
  ' "$listing" || release_fail "$repository $tag source archive contains an unsafe path"
  roots="$(awk -F/ 'NF {print $1}' "$listing" | LC_ALL=C sort -u)"
  if [ -z "$roots" ] || [ "$(wc -l <<< "$roots")" -ne 1 ]; then
    release_fail "$repository $tag source archive must contain one root directory"
  fi
  mkdir -p "$output"
  tar -xzf "$archive" --strip-components=1 -C "$output"
  rm -f "$archive" "$listing"
}

write_component_updates() {
  local unit="$1" repository="$2" previous_tag="$3" current_tag="$4" output="$5"
  if [ -z "$previous_tag" ]; then
    printf "Initial selected version: \`%s\`.\n" "$current_tag" > "$output"
    return
  fi
  if [ "$previous_tag" = "$current_tag" ]; then
    printf "Selected version unchanged: \`%s\`.\n" "$current_tag" > "$output"
    return
  fi

  local comparison status count
  comparison="$(github_api "repos/$repository/compare/$previous_tag...$current_tag")"
  status="$(jq -er '.status | select(. == "ahead" or . == "behind" or . == "diverged" or . == "identical")' \
    <<< "$comparison")" || release_fail "cannot classify $unit update comparison"
  count="$(jq -er '.total_commits' <<< "$comparison")"
  {
    printf "Selected version: \`%s\` → \`%s\`.\n\n" "$previous_tag" "$current_tag"
    case "$status" in
      identical)
        printf 'The version changed without additional repository commits.\n'
        ;;
      behind)
        printf 'The selected version is behind the previous selection; this is a version rollback, not a forward update.\n'
        ;;
      ahead|diverged)
        if [ "$count" -eq 0 ]; then
          printf 'No repository commits are unique to the selected version.\n'
        else
          jq -r '.commits[] |
            ((.commit.message | split("\n")[0] | gsub("`"; "\u0027")) as $subject |
             "- `" + (.sha[0:12]) + "` " + $subject + " ([commit](" + .html_url + "))")' \
            <<< "$comparison"
          if [ "$count" -gt "$(jq '.commits | length' <<< "$comparison")" ]; then
            printf -- '- GitHub truncated the commit list; use the full comparison below.\n'
          fi
        fi
        ;;
    esac
    printf '\n[Full comparison](https://github.com/%s/compare/%s...%s)\n' \
      "$repository" "$previous_tag" "$current_tag"
  } > "$output"
}

validate_component_download() {
  local unit="$1" tag="$2" directory="$3"
  local archive checksum_line checksum_name checksum_value
  archive="$(component_archive "$unit" "$tag")"
  [ -f "$directory/$archive" ] || release_fail "$unit archive is missing"
  [ -f "$directory/SHA256SUMS" ] || release_fail "$unit SHA256SUMS is missing"
  [ "$(grep -cve '^[[:space:]]*$' "$directory/SHA256SUMS")" -eq 1 ] \
    || release_fail "$unit SHA256SUMS must contain exactly one entry"
  checksum_line="$(grep -ve '^[[:space:]]*$' "$directory/SHA256SUMS")"
  read -r checksum_value checksum_name _ <<< "$checksum_line"
  checksum_name="${checksum_name#\*}"
  [ "$checksum_name" = "$archive" ] || release_fail "$unit SHA256SUMS names an unexpected asset"
  [[ "$checksum_value" =~ ^[0-9a-f]{64}$ ]] || release_fail "$unit SHA256SUMS contains an invalid digest"
  [ "$(sha256sum "$directory/$archive" | awk '{print $1}')" = "$checksum_value" ] \
    || release_fail "$unit archive checksum mismatch"
  if tar -tzf "$directory/$archive" \
    | grep -E '(^|/)docs(/|$)|(^|/)test/e2e(/|$)' >/dev/null; then
    release_fail "$unit archive contains docs/ or test/e2e/ content owned by the platform package"
  fi
}

validate_tar_paths() {
  local archive="$1" seen="$2"
  local listing
  listing="$(mktemp)"
  tar -tzf "$archive" > "$listing"
  local raw path
  while IFS= read -r raw; do
    path="${raw#./}"
    [ -n "$path" ] || continue
    [[ "$path" != /* && "$path" != ../* && "$path" != */../* && "$path" != */.. ]] \
      || release_fail "unsafe path in $(basename "$archive"): $raw"
    [[ "$path" == */ ]] && continue
    if grep -Fx "$path" "$seen" >/dev/null; then
      release_fail "release archives contain the same file path: $path"
    fi
    printf '%s\n' "$path" >> "$seen"
  done < "$listing"
  rm -f "$listing"
}

assemble_release() {
  [ "$#" -eq 3 ] || release_fail "usage: aggregate-release.sh assemble <release-version> <fetched-dir> <output-dir>"
  local version="$1" fetched="$2" output="$3"
  validate_aggregate_version "$version"
  [ -d "$fetched/components" ] || release_fail "fetched component directory is missing"
  [ -d "$fetched/sources" ] || release_fail "fetched component source directory is missing"
  assert_safe_output "$output"

  local work expected_selection expected_previous_selection previous platform_bundle platform_name
  work="$(mktemp -d)"
  expected_selection="$work/selection.tsv"
  expected_previous_selection="$work/previous-selection.tsv"
  resolve_selection "$PLATFORM_SOURCE_ROOT" "$version" "$expected_selection"
  previous="$(previous_release "$PLATFORM_SOURCE_ROOT" "$version")"
  : > "$expected_previous_selection"
  if [ -n "$previous" ]; then
    resolve_selection "$PLATFORM_SOURCE_ROOT" "$previous" "$expected_previous_selection"
  fi
  cmp -s "$expected_selection" "$fetched/selection.tsv" \
    || release_fail "fetched component selection does not match the selected platform source"
  cmp -s "$expected_previous_selection" "$fetched/previous-selection.tsv" \
    || release_fail "fetched previous selection does not match the selected platform source"

  platform_bundle="$work/platform-bundle"
  "$ROOT/release/package-platform.sh" package "$version" "$fetched/sources" "$platform_bundle"
  platform_name="$(platform_archive "$version")"
  mkdir -p "$output/assets"
  install -m 0644 "$platform_bundle/assets/$platform_name" "$output/assets/$platform_name"
  install -m 0644 "$expected_selection" "$output/selection.tsv"

  local unit tag archive
  while IFS=$'\t' read -r unit tag; do
    validate_component_download "$unit" "$tag" "$fetched/components/$unit"
    archive="$(component_archive "$unit" "$tag")"
    install -m 0644 "$fetched/components/$unit/$archive" "$output/assets/$archive"
  done < "$expected_selection"

  find "$output/assets" -maxdepth 1 -type f -printf '%f\n' \
    | LC_ALL=C sort > "$work/asset-names"
  (cd "$output/assets" && xargs sha256sum < "$work/asset-names") > "$output/assets/SHA256SUMS"
  write_release_notes "$version" "$previous" "$expected_selection" \
    "$fetched/updates" "$output/release-notes.md"
  validate_bundle "$version" "$output"
  rm -rf "$work"
}

write_release_notes() {
  local version="$1" previous="$2" selection="$3" updates="$4" output="$5" unit tag
  {
    printf '# Kuasar Sandbox %s\n\n' "$version"
    printf 'Kuasar Sandbox is a production-deployable MicroVM sandbox platform for large-scale agent, serverless, and reinforcement-learning workloads. This aggregate contains the platform documentation/test package and the exact component archives validated together on real-KVM BMS.\n\n'
    printf '## Highlights\n\n'
    printf '%s\n' \
      '- Independent Guest Kernel isolation for each MicroVM sandbox.' \
      '- Snapshot templates for creating independent instances, plus stateful pause and resume for the same logical sandbox.' \
      '- Local files, shared file storage, and object storage with layered caches as selectable data paths.' \
      '- Node admission and resource reclamation for safely improving effective sandbox density.' \
      '- Isolated high-speed networking, an E2B-compatible API, and single-node or multi-node deployment.'
    printf '\n## Component versions\n\n'
    printf '| Release unit | Version |\n|---|---|\n'
    while IFS=$'\t' read -r unit tag; do
      printf '| `%s` | `%s` |\n' "$unit" "$tag"
    done < "$selection"
    printf '\nThe five component repositories version and publish independently. `guest-runtime` supplies the `runtime` and `vmlinux` release units but remains one component repository. This aggregate pins all six units as one tested platform combination.\n\n'
    printf '## Supported environment\n\n'
    printf 'Prebuilt assets target Linux x86_64 with glibc 2.38 or newer. A host needs systemd, cgroup v2, writable `/dev/kvm`, and root or non-interactive sudo. Source builds also support `TARGET_ARCH=aarch64`; aarch64 is not part of this prebuilt release.\n\n'
    printf '## Quick Start\n\n'
    printf 'Follow the [Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/%s/docs/quickstart.md) to download all eight explicit assets from this aggregate, verify `SHA256SUMS`, prepare a template, and exercise create, guest exec, pause, reconnect/resume, kill, and cleanup with the unmodified E2B Python SDK.\n\n' "$version"
    printf '## Production deployment\n\n'
    printf 'The system supports production deployment. Operators should complete workload-specific capacity validation and configure production TLS, durable storage, network policy, credentials, monitoring, and recovery for their topology. See [Deployment](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/%s/docs/deployment.md).\n\n' "$version"
    printf '## Known limitations\n\n'
    printf '%s\n' \
      '- Multi-node clusters and centralized network policy require their documented supporting infrastructure.' \
      '- The Quick Start self-signed certificates, local registry, and demo credentials are for evaluation only.' \
      '- Node-local lightweight Egress and OpenTelemetry remain Proposed and are not release prerequisites.'
    printf '\n## Security\n\n'
    printf 'Report vulnerabilities privately through [Report a vulnerability](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new). Do not disclose unpatched vulnerabilities in public issues.\n\n'
    printf '## Versioning and release channels\n\n'
    if is_preview "$version"; then
      printf 'This aggregate is a Preview GitHub prerelease for development and evaluation. It does not replace the current Stable release.\n\n'
    else
      printf 'This aggregate is a Stable, non-prerelease release. Preview continues as the development and evaluation channel. Production readiness and public release-channel stability are separate dimensions.\n\n'
    fi
    if [ -n "$previous" ]; then
      printf "Previous aggregate selection: \`%s\`.\n\n" "$previous"
    elif is_preview "$version"; then
      printf 'This is the first aggregate preview; no earlier aggregate is used as a comparison baseline.\n\n'
    else
      printf 'This is the first formal aggregate release; no preview or repository history is used as a comparison baseline.\n\n'
    fi
    printf '## Component updates\n'
    while IFS=$'\t' read -r unit _; do
      [ -s "$updates/$unit.md" ] || release_fail "component update notes are missing: $unit"
      printf '\n### %s\n\n' "$unit"
      cat "$updates/$unit.md"
    done < "$selection"
    printf "\nVerify every explicit asset with \`SHA256SUMS\`. GitHub provides source archives automatically for this tag.\n"
  } > "$output"
}

expected_asset_names() {
  local version="$1" selection="$2"
  platform_archive "$version"
  while IFS=$'\t' read -r unit tag; do
    component_archive "$unit" "$tag"
  done < "$selection"
  printf 'SHA256SUMS\n'
}

validate_bundle() {
  [ "$#" -eq 2 ] || release_fail "usage: aggregate-release.sh validate <release-version> <bundle-dir>"
  local version="$1" bundle="$2"
  validate_aggregate_version "$version"
  [ -d "$bundle/assets" ] || release_fail "aggregate assets directory is missing"
  [ -s "$bundle/release-notes.md" ] || release_fail "aggregate release notes are missing"
  [ -f "$bundle/selection.tsv" ] || release_fail "aggregate selection is missing"
  local expected_selection expected_names actual_names
  expected_selection="$(mktemp)"
  expected_names="$(mktemp)"
  actual_names="$(mktemp)"
  resolve_selection "$PLATFORM_SOURCE_ROOT" "$version" "$expected_selection"
  cmp -s "$expected_selection" "$bundle/selection.tsv" \
    || release_fail "aggregate selection does not match project repository main"
  local heading
  for heading in \
    "# Kuasar Sandbox $version" \
    '## Highlights' \
    '## Component versions' \
    '## Supported environment' \
    '## Quick Start' \
    '## Production deployment' \
    '## Known limitations' \
    '## Security' \
    '## Versioning and release channels' \
    '## Component updates'; do
    grep -Fqx "$heading" "$bundle/release-notes.md" \
      || release_fail "aggregate release notes are missing $heading"
  done
  local unit tag
  while IFS=$'\t' read -r unit tag; do
    grep -Fqx "| \`$unit\` | \`$tag\` |" "$bundle/release-notes.md" \
      || release_fail "aggregate release notes are missing the $unit version"
    grep -Fqx "### $unit" "$bundle/release-notes.md" \
      || release_fail "aggregate release notes are missing $unit updates"
  done < "$expected_selection"
  expected_asset_names "$version" "$expected_selection" | LC_ALL=C sort > "$expected_names"
  find "$bundle/assets" -mindepth 1 -maxdepth 1 -type f -printf '%f\n' | LC_ALL=C sort > "$actual_names"
  cmp -s "$expected_names" "$actual_names" \
    || { diff -u "$expected_names" "$actual_names" >&2 || true; release_fail "aggregate contains an unexpected asset set"; }
  (cd "$bundle/assets" && sha256sum --quiet -c SHA256SUMS) \
    || release_fail "aggregate SHA256SUMS validation failed"
  "$ROOT/release/package-platform.sh" validate "$version" \
    "$bundle/assets/$(platform_archive "$version")"

  local seen tag archive
  seen="$(mktemp)"
  : > "$seen"
  validate_tar_paths "$bundle/assets/$(platform_archive "$version")" "$seen"
  while IFS=$'\t' read -r unit tag; do
    archive="$(component_archive "$unit" "$tag")"
    validate_tar_paths "$bundle/assets/$archive" "$seen"
  done < "$expected_selection"
  rm -f "$seen"
  rm -f "$expected_selection" "$expected_names" "$actual_names"
}

extract_bundle() {
  [ "$#" -eq 3 ] || release_fail "usage: aggregate-release.sh extract <release-version> <bundle-dir> <install-dir>"
  local version="$1" bundle="$2" install="$3"
  validate_bundle "$version" "$bundle"
  assert_safe_output "$install"
  mkdir -p "$install"
  local name
  while IFS= read -r name; do
    [ "$name" = SHA256SUMS ] && continue
    tar -xzf "$bundle/assets/$name" -C "$install"
  done < <(expected_asset_names "$version" "$bundle/selection.tsv")
}

case "${1:-}" in
  fetch) shift; fetch_components "$@" ;;
  assemble) shift; assemble_release "$@" ;;
  validate) shift; validate_bundle "$@" ;;
  extract) shift; extract_bundle "$@" ;;
  *) release_fail "usage: aggregate-release.sh <fetch|assemble|validate|extract> ..." ;;
esac
