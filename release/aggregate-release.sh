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
  python3 "$ROOT/release/selection.py" "$PLATFORM_SOURCE_ROOT" "$version" --new > "$output/selection.tsv"
  local previous
  previous="$(previous_release "$PLATFORM_SOURCE_ROOT" "$version")"
  : > "$output/previous-selection.tsv"
  if [ -n "$previous" ]; then
    ensure_aggregate_tag "$PLATFORM_SOURCE_ROOT" "$previous"
    resolve_selection "$PLATFORM_SOURCE_ROOT" "$previous" "$output/previous-selection.tsv"
  fi

  local unit tag previous_tag repository archive arm_archive release_state expected_prerelease unit_dir
  while IFS=$'\t' read -r unit tag; do
    previous_tag="$(awk -F '\t' -v unit="$unit" '$1 == unit {print $2}' \
      "$output/previous-selection.tsv")"
    repository="$(component_repository "$unit")"
    archive="$(component_archive "$unit" "$tag")"
    arm_archive="$(component_archive "$unit" "$tag" aarch64)"
    unit_dir="$output/components/$unit"
    mkdir -p "$unit_dir"
    release_state="$(github_api "repos/$repository/releases/tags/$tag")"
    expected_prerelease=false
    is_preview "$tag" && expected_prerelease=true
    jq -e \
      --arg tag "$tag" \
      --arg archive "$archive" --arg arm "$arm_archive" \
      --argjson prerelease "$expected_prerelease" '
        .tag_name == $tag
        and .draft == false
        and .prerelease == $prerelease
        and (([.assets[] | select(.name != "SHA256SUMS") | .name] | sort) as $names |
          $names == ([$archive] | sort) or
          $names == ([$archive, $arm] | sort))
        and ([.assets[] | select(.name != "SHA256SUMS")] | all(.state == "uploaded" and (.id | type == "number" and . > 0) and (.size | type == "number" and . > 0) and (.digest | type == "string" and test("^sha256:[0-9a-f]{64}$"))))
      ' <<< "$release_state" >/dev/null \
      || release_fail "$repository $tag does not satisfy its single/dual architecture release contract"

    jq '[.assets[] | select(.name != "SHA256SUMS") | {id, name, size, digest, state}]' <<< "$release_state" > "$unit_dir/assets.json"
    local name asset
    while IFS= read -r name; do
      asset="$(jq -ce --arg name "$name" '.assets[] | select(.name == $name)' <<< "$release_state")"
      github_download_asset "$repository" "$(jq -er '.id' <<< "$asset")" "$unit_dir/$name"
      verify_github_asset "$unit_dir/$name" "$asset"
    done < <(jq -r '.[] | .name' "$unit_dir/assets.json")
    validate_component_download "$unit" "$tag" "$unit_dir"
    local source_sha
    source_sha="$(github_api "repos/$repository/git/ref/tags/$tag" \
      | jq -er '.object | select(.type == "commit") | .sha | select(test("^[0-9a-f]{40}$"))')"
    python3 -B "$ROOT/release/tag_sources.py" fetch "$unit" "$tag" "$source_sha" "$output/sources/$unit" > /dev/null
    # Human release prose is already committed in the selected manifest.
    # Fetching assets must not regenerate a different publication description.
  done < "$output/selection.tsv"
  python3 -B "$ROOT/release/tag_sources.py" inspect "$PLATFORM_SOURCE_ROOT" "$version" "$output/sources" \
    > "$output/source-records.json"
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

  if [ "$unit" = vmlinux ]; then
    python3 -B "$ROOT/release/vmlinux_updates.py" "$6" \
      "$previous_tag" "$current_tag" > "$output"
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
  local archive arch
  local arches=(x86_64)
  if [ -f "$directory/$(component_archive "$unit" "$tag" aarch64)" ]; then arches+=(aarch64); fi
  [ -f "$directory/assets.json" ] || release_fail "$unit download asset identities are missing"
  [ "$(jq length "$directory/assets.json")" -eq "${#arches[@]}" ] || release_fail "$unit has an incomplete downloaded asset set"
  for arch in "${arches[@]}"; do
  archive="$(component_archive "$unit" "$tag" "$arch")"
  [ -f "$directory/$archive" ] || release_fail "$unit archive is missing"
  local asset
  asset=$(jq -ce --arg name "$archive" '[.[] | select(.name == $name)] | select(length == 1) | .[0]' "$directory/assets.json") \
    || release_fail "$unit downloaded asset identity is missing or duplicated"
  verify_github_asset "$directory/$archive" "$asset"
  if tar -tzf "$directory/$archive" \
    | grep -E '(^|/)docs(/|$)|(^|/)test/e2e(/|$)' >/dev/null; then
    release_fail "$unit archive contains docs/ or test/e2e/ content owned by the platform package"
  fi
  if tar -tzf "$directory/$archive" | awk -v unit="$unit" '
    {
      path=$0
      sub(/^\.\//, "", path)
      if (path ~ /^share\/(licenses|sources)\/[^/]+/ &&
          path !~ ("^share/(licenses|sources)/" unit "(/|$)")) {
        exit 1
      }
    }
  '; then
    :
  else
    release_fail "$unit archive contains another release unit's material namespace"
  fi
  done
}

validate_tar_paths() {
  local archive="$1" seen="$2"
  python3 -B "$ROOT/release/archive_paths.py" validate "$seen" "$archive"
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
  python3 "$ROOT/release/selection.py" "$PLATFORM_SOURCE_ROOT" "$version" --new > "$expected_selection"
  previous="$(previous_release "$PLATFORM_SOURCE_ROOT" "$version")"
  : > "$expected_previous_selection"
  if [ -n "$previous" ]; then
    ensure_aggregate_tag "$PLATFORM_SOURCE_ROOT" "$previous"
    resolve_selection "$PLATFORM_SOURCE_ROOT" "$previous" "$expected_previous_selection"
  fi
  cmp -s "$expected_selection" "$fetched/selection.tsv" \
    || release_fail "fetched component selection does not match the selected platform source"
  cmp -s "$expected_previous_selection" "$fetched/previous-selection.tsv" \
    || release_fail "fetched previous selection does not match the selected platform source"
  python3 -B "$ROOT/release/tag_sources.py" inspect "$PLATFORM_SOURCE_ROOT" "$version" "$fetched/sources" \
    --evidence "$fetched/source-records.json" > "$work/source-records.json"

  platform_bundle="$work/platform-bundle"
  E2E_HELPER_ROOT="${E2E_HELPER_ROOT:-$fetched/e2e-helpers}" \
    E2E_WHEEL_ROOT="${E2E_WHEEL_ROOT:-$fetched/e2e-wheels}" \
    "$ROOT/release/package-platform.sh" package "$version" "$fetched/sources" "$platform_bundle"
  platform_name="$(platform_archive "$version")"
  python3 "$ROOT/release/validate-e2e-package.py" "$platform_bundle/assets/$platform_name" --publication
  mkdir -p "$output/assets"
  install -m 0644 "$platform_bundle/assets/$platform_name" "$output/assets/$platform_name"
  install -m 0644 "$expected_selection" "$output/selection.tsv"
  install -m 0644 "$work/source-records.json" "$output/source-records.json"

  local unit tag archive arch
  while IFS=$'\t' read -r unit tag; do
    validate_component_download "$unit" "$tag" "$fetched/components/$unit"
    for arch in x86_64 aarch64; do
      archive="$(component_archive "$unit" "$tag" "$arch")"
      if [ -f "$fetched/components/$unit/$archive" ]; then
        install -m 0644 "$fetched/components/$unit/$archive" "$output/assets/$archive"
      fi
    done
  done < "$expected_selection"

  local delivery source_sha
  delivery=$(release_delivery "$PLATFORM_SOURCE_ROOT" "$version")
  [ "$delivery" = workbench-v1 ] || release_fail 'new aggregate assembly requires delivery: workbench-v1 in the selected manifest'
  source_sha=$(python3 "$ROOT/release/selection.py" "$PLATFORM_SOURCE_ROOT" "$version" --commit)
  python3 -B "$ROOT/release/workbench_assets.py" "$fetched/workbench" "$version" "$source_sha" > "$work/workbench.json"
  mkdir -p "$output/workbench"
  for arch in x86_64 aarch64; do
    install -m 0644 "$fetched/workbench/$(workbench_archive "$version" "$arch")" "$output/assets/"
    install -m 0644 "$fetched/workbench/workbench-$arch.json" "$output/workbench/"
  done

  python3 -B "$ROOT/release/release_notes.py" export "$PLATFORM_SOURCE_ROOT" \
    "$version" "$output/release-notes.md"
  validate_bundle "$version" "$output" --publication
  rm -rf "$work"
}

write_release_notes() {
  local version="$1" previous="$2" selection="$3" updates="$4" output="$5" unit tag manifest=release.yaml
  if is_preview "$version"; then manifest=daily-preview.yaml; fi
  {
    printf '# Kuasar Sandbox %s\n\n' "$version"
    if is_preview "$version"; then
      printf 'This aggregate is a Preview GitHub prerelease for development and evaluation. It does not replace the current Stable release.\n\n'
    else
      printf 'This aggregate is a Stable, non-prerelease release. Channel status does not establish workload-specific production readiness.\n\n'
    fi
    if [ -n "$previous" ]; then
      printf "Previous aggregate selection: \`%s\`.\n\n" "$previous"
    elif is_preview "$version"; then
      printf 'This is the first aggregate preview; no earlier aggregate is used as a comparison baseline.\n\n'
    else
      printf 'This is the first formal aggregate release; no preview or repository history is used as a comparison baseline.\n\n'
    fi
    printf 'Selection and editable release notes: [`releases/%s`](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/%s/releases/%s).\n\n' "$manifest" "$version" "$manifest"
    printf '## Component versions\n\n'
    printf '| Release unit | Version |\n|---|---|\n'
    while IFS=$'\t' read -r unit tag; do
      printf '| `%s` | `%s` |\n' "$unit" "$tag"
    done < "$selection"
    printf '\nSix release units from five independently versioned repositories; `guest-runtime` supplies both `runtime` and `vmlinux`.\n\n'
    printf '## Component updates\n'
    while IFS=$'\t' read -r unit _; do
      [ -s "$updates/$unit.md" ] || release_fail "component update notes are missing: $unit"
      printf '\n### %s\n\n' "$unit"
      cat "$updates/$unit.md"
    done < "$selection"
    printf '\n## Download and use\n\n'
    printf '%s\n' \
      '- Choose native Linux x86_64 or aarch64 assets. Follow the download guide to verify GitHub asset sizes and SHA-256 digests before use.' \
      '- Extract the six product archives and `platform-release` together. The platform archive contains guides, Demo, tests, helpers and locked wheels.' \
      '- Workbench is a separate compressed Docker image archive: import it with `docker load -i`; do not extract it into products.'
    printf '\nSee [Quick Start](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/%s/docs/quickstart.md) for host prerequisites and sandbox lifecycle examples, and [Deployment](https://github.com/kuasar-sandbox/kuasar-sandbox/blob/%s/docs/deployment.md) for TLS, storage, network policy, credentials and recovery. Demo credentials and self-signed certificates are for evaluation only.\n\n' "$version" "$version"
    printf '## Security\n\n'
    printf 'Workbench system mode is a trusted administrator environment with Docker privileged access. Private namespaces/data and owned cleanup do not provide hostile-root confinement.\n\n'
    printf 'Report vulnerabilities privately through [Report a vulnerability](https://github.com/kuasar-sandbox/kuasar-sandbox/security/advisories/new), not public issues.\n'
  } > "$output"
  # This generator is called before manifest admission, never by the publisher.
  # The complete result is reviewed and persisted in notes.body.
}


# Existing x86-only releases retain their original contract. A dual aggregate
# contains every ARM unit; a partially populated target is never accepted.
bundle_arches() {
  local bundle=$1 version=$2
  if [ "$(release_delivery "$PLATFORM_SOURCE_ROOT" "$version")" = workbench-v1 ]; then
    printf 'x86_64\naarch64\n'
    return
  fi
  printf 'x86_64\n'
  if find "$bundle/assets" -maxdepth 1 -name '*aarch64*.tar.gz' | grep -q .; then printf 'aarch64\n'; fi
}

expected_asset_names() {
  local version="$1" selection="$2" arch
  shift 2
  local arches=("${@:-x86_64}")
  platform_archive "$version"
  for arch in "${arches[@]}"; do
    while IFS=$'\t' read -r unit tag; do
      component_archive "$unit" "$tag" "$arch"
    done < "$selection"
  done
  if [ "$(release_delivery "$PLATFORM_SOURCE_ROOT" "$version")" = workbench-v1 ]; then
    for arch in "${arches[@]}"; do workbench_archive "$version" "$arch"; done
  fi
}

validate_bundle() {
  [ "$#" -eq 2 ] || { [ "$#" -eq 3 ] && { [ "$3" = --publication ] || [ "$3" = --historical ]; }; } \
    || release_fail "usage: aggregate-release.sh validate <release-version> <bundle-dir> [--publication|--historical]"
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
    || release_fail "aggregate selection does not match the selected version manifest"
  if [ "${3:-}" != --historical ]; then
    python3 -B "$ROOT/release/release_notes.py" validate "$PLATFORM_SOURCE_ROOT" \
      "$version" "$bundle/release-notes.md"
  fi
  local unit tag
  local arches=() arch
  mapfile -t arches < <(bundle_arches "$bundle" "$version")
  expected_asset_names "$version" "$expected_selection" "${arches[@]}" | LC_ALL=C sort > "$expected_names"
  find "$bundle/assets" -mindepth 1 -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | LC_ALL=C sort > "$actual_names"
  if [ "${3:-}" = --publication ] && [ -e "$bundle/assets/SHA256SUMS" ]; then release_fail "new releases must not publish SHA256SUMS"; fi
  cmp -s "$expected_names" "$actual_names" \
    || { diff -u "$expected_names" "$actual_names" >&2 || true; release_fail "aggregate contains an unexpected asset set"; }
  "$ROOT/release/package-platform.sh" validate "$version" \
    "$bundle/assets/$(platform_archive "$version")" "$(release_delivery "$PLATFORM_SOURCE_ROOT" "$version")"

  if [ "$(release_delivery "$PLATFORM_SOURCE_ROOT" "$version")" = workbench-v1 ]; then
    local source_sha
    source_sha=$(python3 "$ROOT/release/selection.py" "$PLATFORM_SOURCE_ROOT" "$version" --commit)
    python3 -B "$ROOT/release/workbench_assets.py" "$bundle/assets" "$version" "$source_sha" --receipts "$bundle/workbench" >/dev/null
  fi

  local seen tag archive
  seen="$(mktemp)"
  for arch in "${arches[@]}"; do
    : > "$seen"
    validate_tar_paths "$bundle/assets/$(platform_archive "$version")" "$seen"
    while IFS=$'\t' read -r unit tag; do
      archive="$(component_archive "$unit" "$tag" "$arch")"
      validate_tar_paths "$bundle/assets/$archive" "$seen"
    done < "$expected_selection"
  done
  rm -f "$seen"
  rm -f "$expected_selection" "$expected_names" "$actual_names"
}

extract_bundle() {
  [ "$#" -eq 3 ] || [ "$#" -eq 4 ] || release_fail "usage: aggregate-release.sh extract <release-version> <bundle-dir> <install-dir> [arch]"
  local version="$1" bundle="$2" install="$3" arch="${4:-x86_64}"
  case "$arch" in x86_64|aarch64) ;; *) release_fail 'unsupported extraction architecture' ;; esac
  validate_bundle "$version" "$bundle" --historical
  assert_safe_output "$install"
  local name archives=()
  while IFS= read -r name; do
    [ "$name" = SHA256SUMS ] && continue
    [[ "$name" = workbench-* ]] && continue # Docker import is separate from release extraction.
    archives+=("$bundle/assets/$name")
  done < <(expected_asset_names "$version" "$bundle/selection.tsv" "$arch")
  python3 -B "$ROOT/release/archive_paths.py" extract "$install" "${archives[@]}"
}

case "${1:-}" in
  fetch) shift; fetch_components "$@" ;;
  assemble) shift; assemble_release "$@" ;;
  validate) shift; validate_bundle "$@" ;;
  extract) shift; extract_bundle "$@" ;;
  render-notes) shift; write_release_notes "$@" ;;
  *) release_fail "usage: aggregate-release.sh <fetch|assemble|validate|extract> ..." ;;
esac
