#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"

readonly INFRA_FAILURE_RE='(The self-hosted runner.*lost communication|runner.*(offline|lost)|No space left on device|Connection (timed out|reset)|TLS handshake timeout|Temporary failure|Could not resolve host|unexpected EOF|HTTP (429|500|502|503|504)|failed to download|rate limit|e2e: (timed out waiting for zot|zot failed to start after [0-9]+ attempts))'
readonly COMPONENT_ROWS=(
  'accelerator|kuasar-sandbox/accelerator|release.yml'
  'connector|kuasar-sandbox/connector|release.yml'
  'sandboxer|kuasar-sandbox/sandboxer|release.yml'
  'orchestrator|kuasar-sandbox/orchestrator|component-release.yml'
  'vmlinux|kuasar-sandbox/guest-runtime|release-vmlinux.yml'
)

api_optional() {
  local endpoint="$1" output="$2"
  if gh api "$endpoint" > "$output" 2> "$TMP/api-error"; then
    return 0
  fi
  if grep -q '(HTTP 404)' "$TMP/api-error"; then
    : > "$output"
    return 4
  fi
  cat "$TMP/api-error" >&2
  return 1
}

stable_base_version() {
  local config="$ROOT/releases/daily-preview.yaml" value
  [ -f "$config" ] || release_fail "daily preview configuration is missing"
  value="$(awk '$1 == "version:" {print $2}' "$config")"
  [ "$(awk '$1 == "version:" {count++} END {print count + 0}' "$config")" -eq 1 ] \
    || release_fail "daily preview configuration must contain one version"
  validate_aggregate_version "$value"
  is_preview "$value" && release_fail "daily preview base version must be stable"
  printf '%s\n' "$value"
}

preview_state_version() {
  local config="$ROOT/releases/daily-preview.yaml" value
  value="$(awk '$1 == "preview_version:" {print $2}' "$config")"
  [ "$(awk '$1 == "preview_version:" {count++} END {print count + 0}' "$config")" -eq 1 ] \
    || release_fail "daily preview configuration must contain one preview_version"
  [[ "$value" =~ ^preview\.[0-9]{8}$ ]] \
    || release_fail "daily preview preview_version must match preview.YYYYMMDD"
  printf '%s\n' "$value"
}

current_preview_version() {
  printf '%s-%s\n' "$(stable_base_version)" "$(preview_state_version)"
}

release_exists() {
  local repository="$1" tag="$2" output="$3"
  if api_optional "repos/$repository/releases/tags/$tag" "$output"; then
    jq -e --arg tag "$tag" '.tag_name == $tag and .draft == false' "$output" >/dev/null \
      || release_fail "$repository $tag exists but is not a published release"
    return 0
  else
    local rc=$?
    [ "$rc" -eq 4 ] \
      || release_fail "cannot query release state: $repository $tag"
  fi
  return 1
}

validate_component_release_state() {
  local unit="$1" tag="$2" state="$3" archive prerelease
  archive="$(component_archive "$unit" "$tag")"
  prerelease=false
  is_preview "$tag" && prerelease=true
  jq -e --arg tag "$tag" --arg archive "$archive" --argjson prerelease "$prerelease" '
      .tag_name == $tag
      and .draft == false
      and .prerelease == $prerelease
      and (.assets | length == 2)
      and ([.assets[].name] | sort == (["SHA256SUMS", $archive] | sort))
    ' "$state" >/dev/null || release_fail "$unit $tag violates the component release contract"
}

latest_workflow_run() {
  local repository="$1" workflow="$2" title="$3" output="$4"
  gh api --paginate --slurp \
    "repos/$repository/actions/workflows/$workflow/runs?event=workflow_dispatch&per_page=100" \
    | jq --arg title "$title" \
      '[.[].workflow_runs[] | select(.display_title == $title)] | sort_by(.created_at) | last // empty' \
      > "$output"
  [ -s "$output" ] && [ "$(cat "$output")" != null ]
}

rerun_infrastructure_failure() {
  local repository="$1" run="$2"
  local id conclusion attempt logs
  id="$(jq -er '.id' "$run")"
  conclusion="$(jq -er '.conclusion' "$run")"
  attempt="$(jq -er '.run_attempt' "$run")"
  [ "$attempt" -lt 3 ] || release_fail "$repository workflow run $id exhausted infrastructure retries"
  case "$conclusion" in
    cancelled|timed_out|stale)
      ;;
    failure)
      logs="$TMP/run-$id.log"
      if ! gh run view "$id" --repo "$repository" --log-failed > "$logs" 2>&1; then
        release_fail "cannot inspect failed workflow run $repository#$id"
      fi
      grep -Eiq "$INFRA_FAILURE_RE" "$logs" \
        || release_fail "$repository workflow run $id failed for a non-infrastructure reason"
      ;;
    *) release_fail "$repository workflow run $id concluded as $conclusion" ;;
  esac
  echo "==> rerun infrastructure failure: $repository#$id (attempt $attempt)"
  if [ "$conclusion" = failure ]; then
    gh run rerun "$id" --repo "$repository" --failed
  else
    gh run rerun "$id" --repo "$repository"
  fi
}

ensure_workflow() {
  local repository="$1" workflow="$2" title="$3"
  shift 3
  local run="$TMP/run.json"
  if ! latest_workflow_run "$repository" "$workflow" "$title" "$run"; then
    echo "==> dispatch $repository/$workflow: $title"
    gh workflow run "$workflow" --repo "$repository" --ref main "$@"
    return 10
  fi
  case "$(jq -er '.status' "$run")" in
    queued|in_progress|pending|requested|waiting)
      echo "==> pending $repository run $(jq -r '.html_url' "$run")"
      return 10
      ;;
    completed)
      if [ "$(jq -er '.conclusion' "$run")" = success ]; then
        release_fail "$repository workflow succeeded but did not publish the expected release: $title"
      fi
      rerun_infrastructure_failure "$repository" "$run"
      return 10
      ;;
    *) release_fail "unexpected workflow status for $repository: $(jq -er '.status' "$run")" ;;
  esac
}

ensure_component() {
  local unit="$1" tag="$2" repository="$3" workflow="$4"
  local state="$TMP/release-$unit.json"
  if release_exists "$repository" "$tag" "$state"; then
    validate_component_release_state "$unit" "$tag" "$state"
    echo "==> reuse $repository $tag"
    return 0
  fi
  local args=(-f "version=$tag")
  if [ "$unit" = runtime ]; then
    args+=(-f "sandboxer_version=$SANDBOXER_TAG")
  fi
  ensure_workflow "$repository" "$workflow" "Release $tag" "${args[@]}"
}

unit_release_pattern() {
  case "$1" in
    accelerator|connector|sandboxer|orchestrator)
      printf '%s\n' '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$'
      ;;
    runtime)
      printf '%s\n' '^runtime-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$'
      ;;
    vmlinux)
      printf '%s\n' '^vmlinux-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8})?$'
      ;;
    *) release_fail "unknown release unit: $1" ;;
  esac
}

preview_component_tag() {
  local unit="$1" stable_component="$2" date="$3"
  case "$unit" in
    accelerator|connector|sandboxer|orchestrator)
      printf '%s-preview.%s\n' "$stable_component" "$date"
      ;;
    runtime|vmlinux)
      printf '%s-%s-preview.%s\n' "$unit" "$stable_component" "$date"
      ;;
    *) release_fail "unknown release unit: $unit" ;;
  esac
}

latest_component_release() {
  local unit="$1" repository="$2" output="$3" pattern
  pattern="$(unit_release_pattern "$unit")"
  gh api --paginate --slurp "repos/$repository/releases?per_page=100" \
    | jq --arg pattern "$pattern" '
        [ .[][]
          | select(.draft == false and .published_at != null)
          | select(.tag_name | test($pattern)) ]
        | sort_by(.published_at)
        | last // empty
      ' > "$output"
  [ -s "$output" ] && [ "$(cat "$output")" != null ]
}

select_preview_unit() {
  local unit="$1" repository="$2" candidate="$3" output="$4"
  local state="$TMP/select-candidate-$unit.json"
  if release_exists "$repository" "$candidate" "$state"; then
    validate_component_release_state "$unit" "$candidate" "$state"
    printf '%s\n' "$candidate" > "$output"
    return
  fi

  local latest="$TMP/select-latest-$unit.json"
  if ! latest_component_release "$unit" "$repository" "$latest"; then
    printf '%s\n' "$candidate" > "$output"
    return
  fi
  local latest_tag head comparison status
  latest_tag="$(jq -er '.tag_name' "$latest")"
  validate_component_release_state "$unit" "$latest_tag" "$latest"
  head="$(github_api "repos/$repository/commits/main" | jq -er '.sha')"
  comparison="$(github_api "repos/$repository/compare/$latest_tag...$head")"
  status="$(jq -er '.status' <<< "$comparison")"
  case "$status" in
    identical)
      printf '%s\n' "$latest_tag" > "$output"
      ;;
    ahead)
      printf '%s\n' "$candidate" > "$output"
      ;;
    behind|diverged)
      release_fail "$repository main is $status relative to latest release $latest_tag"
      ;;
    *) release_fail "cannot classify $repository main against $latest_tag" ;;
  esac
}

persist_preview_manifest() {
  local version="$1" manifest="$2" path="releases/daily-preview.yaml"
  [ -n "${PLATFORM_TOKEN:-}" ] \
    || release_fail "PLATFORM_TOKEN is required to commit generated preview manifests"
  local request="$TMP/manifest-request.json" response="$TMP/manifest-response.json"
  local remote_state="$TMP/manifest-remote.json" remote="$TMP/manifest-remote.yaml" sha
  GH_TOKEN="$PLATFORM_TOKEN" gh api \
    "repos/kuasar-sandbox/kuasar-sandbox/contents/$path?ref=main" > "$remote_state"
  jq -er '.content' "$remote_state" | tr -d '\n' | base64 -d > "$remote"
  sha="$(jq -er '.sha' "$remote_state")"
  if cmp -s "$manifest" "$remote"; then
    install -m 0644 "$manifest" "$ROOT/$path"
    echo "==> reuse concurrently committed $path"
    return
  fi
  cmp -s "$ROOT/$path" "$remote" \
    || release_fail "$path changed on main while this coordinator was selecting components"
  jq -n \
    --arg message "release: select daily preview ${version#release-}" \
    --arg content "$(base64 -w0 "$manifest")" \
    --arg branch main \
    --arg sha "$sha" \
    '{message: $message, content: $content, branch: $branch, sha: $sha}' > "$request"
  if GH_TOKEN="$PLATFORM_TOKEN" gh api --method PUT \
      "repos/kuasar-sandbox/kuasar-sandbox/contents/$path" --input "$request" > "$response"; then
    install -m 0644 "$manifest" "$ROOT/$path"
    echo "==> committed $path at $(jq -er '.commit.sha' "$response")"
    return
  fi

  GH_TOKEN="$PLATFORM_TOKEN" gh api \
    "repos/kuasar-sandbox/kuasar-sandbox/contents/$path?ref=main" > "$remote_state"
  jq -er '.content' "$remote_state" | tr -d '\n' | base64 -d > "$remote"
  cmp -s "$manifest" "$remote" \
    || release_fail "$path was concurrently committed with a different selection"
  install -m 0644 "$manifest" "$ROOT/$path"
  echo "==> reuse concurrently committed $path"
}

generate_preview_manifest() {
  local stable_component="$1" date="$2" version="$3" current_selection="$4" result="$5"
  local current_preview
  current_preview="$(preview_state_version)"

  local unit repository candidate selected
  for unit in "${RELEASE_UNITS[@]}"; do
    repository="$(component_repository "$unit")"
    candidate="$(preview_component_tag "$unit" "$stable_component" "$date")"
    selected="$TMP/selected-$unit"
    select_preview_unit "$unit" "$repository" "$candidate" "$selected"
  done

  local selection_changed=false selected_tag current_tag
  for unit in "${RELEASE_UNITS[@]}"; do
    selected_tag="$(cat "$TMP/selected-$unit")"
    current_tag="$(awk -F '\t' -v unit="$unit" '$1 == unit {print $2}' "$current_selection")"
    if [ "$selected_tag" != "$current_tag" ]; then
      selection_changed=true
    fi
  done
  if [ "$selection_changed" = false ]; then
    printf 'unchanged\n' > "$result"
    echo "==> no component changes since $(current_preview_version); keep maintained preview"
    return
  fi

  local manifest="$TMP/daily-preview.yaml" previous_version
  previous_version="$(awk '$1 == "previous_version:" {print $2}' \
    "$ROOT/releases/daily-preview.yaml")"
  {
    printf 'version: release-%s\n' "$stable_component"
    if [ -n "$previous_version" ]; then
      printf 'previous_version: %s\n' "$previous_version"
    fi
    printf 'preview_version: preview.%s\n' "$date"
    printf 'previous_preview_version: %s\n' "$current_preview"
    printf 'components:\n'
    for unit in "${RELEASE_UNITS[@]}"; do
      printf '  %s: %s\n' "$unit" "$(cat "$TMP/selected-$unit")"
    done
  } > "$manifest"
  persist_preview_manifest "$version" "$manifest"
  resolve_selection "$ROOT" "$version" "$TMP/generated-selection-check.tsv"
  printf 'changed\n' > "$result"
}

formal_release_started() {
  local stable_aggregate="$1" selection="$TMP/stable-selection.tsv" unit tag repository
  resolve_selection "$ROOT" "$stable_aggregate" "$selection"
  if release_exists kuasar-sandbox/kuasar-sandbox "$stable_aggregate" "$TMP/stable-platform.json"; then
    echo "==> stable aggregate $stable_aggregate is published; daily previews are complete"
    return 0
  fi
  while IFS=$'\t' read -r unit tag; do
    repository="$(component_repository "$unit")"
    if release_exists "$repository" "$tag" "$TMP/stable-$unit.json"; then
      echo "==> stable component $repository $tag exists; daily preview is paused during formal release"
      return 0
    fi
  done < "$selection"
  return 1
}

converge_once() {
  local pending=0 row unit repository workflow tag rc
  for row in "${COMPONENT_ROWS[@]}"; do
    IFS='|' read -r unit repository workflow <<< "$row"
    tag="$(awk -F '\t' -v unit="$unit" '$1 == unit {print $2}' "$SELECTION")"
    rc=0
    ensure_component "$unit" "$tag" "$repository" "$workflow" || rc=$?
    if [ "$rc" -ne 0 ]; then
      [ "$rc" -eq 10 ] || return "$rc"
      pending=1
    fi
  done
  if [ "$pending" -ne 0 ]; then
    return 10
  fi

  rc=0
  ensure_component runtime "$RUNTIME_TAG" kuasar-sandbox/guest-runtime release-runtime.yml || rc=$?
  if [ "$rc" -ne 0 ]; then
    [ "$rc" -eq 10 ] || return "$rc"
    return 10
  fi

  local platform_state="$TMP/platform-release.json"
  if release_exists kuasar-sandbox/kuasar-sandbox "$AGGREGATE_VERSION" "$platform_state"; then
    echo "==> aggregate $AGGREGATE_VERSION is published"
    return 0
  fi
  rc=0
  ensure_workflow kuasar-sandbox/kuasar-sandbox aggregate-release.yml \
    "Aggregate $AGGREGATE_VERSION" -f "version=$AGGREGATE_VERSION" || rc=$?
  if [ "$rc" -ne 0 ]; then
    [ "$rc" -eq 10 ] || return "$rc"
    return 10
  fi
}

main() {
  local requested_date="${1:-}" stable_aggregate stable_component today date
  local current_aggregate current_date current_state
  stable_aggregate="$(stable_base_version)"
  stable_component="${stable_aggregate#release-}"
  if [ "$requested_date" = --version ]; then
    [ "$#" -eq 2 ] || release_fail "usage: preview-coordinator.sh --version <release-version>"
    AGGREGATE_VERSION="$2"
    validate_aggregate_version "$AGGREGATE_VERSION"
    SELECTION="$TMP/selection.tsv"
    resolve_selection "$ROOT" "$AGGREGATE_VERSION" "$SELECTION"
    SANDBOXER_TAG="$(awk -F '\t' '$1 == "sandboxer" {print $2}' "$SELECTION")"
    RUNTIME_TAG="$(awk -F '\t' '$1 == "runtime" {print $2}' "$SELECTION")"
    echo "==> converge $AGGREGATE_VERSION"
    converge_until_deadline
    return
  fi
  [ "$#" -le 1 ] || release_fail "usage: preview-coordinator.sh [YYYYMMDD]"
  current_aggregate="$(current_preview_version)"
  resolve_selection "$ROOT" "$current_aggregate" "$TMP/current-selection-check.tsv"
  if formal_release_started "$stable_aggregate"; then
    return 0
  fi
  today="$(TZ=Asia/Shanghai date +%Y%m%d)"
  if [ -n "$requested_date" ]; then
    [[ "$requested_date" =~ ^[0-9]{8}$ ]] || release_fail "date must match YYYYMMDD"
    date="$requested_date"
  else
    date="$today"
  fi

  current_date="${current_aggregate##*.}"
  current_state="$TMP/current-platform-release.json"
  if release_exists kuasar-sandbox/kuasar-sandbox "$current_aggregate" "$current_state"; then
    if [ "$date" -lt "$current_date" ]; then
      release_fail "requested preview date $date is older than maintained preview $current_date"
    fi
    if [ "$date" -gt "$current_date" ]; then
      AGGREGATE_VERSION="release-$stable_component-preview.$date"
      validate_aggregate_version "$AGGREGATE_VERSION"
      local generation_result="$TMP/preview-generation-result"
      generate_preview_manifest "$stable_component" "$date" "$AGGREGATE_VERSION" \
        "$TMP/current-selection-check.tsv" "$generation_result"
      case "$(cat "$generation_result")" in
        changed) ;;
        unchanged) AGGREGATE_VERSION="$current_aggregate" ;;
        *) release_fail "unexpected preview generation result" ;;
      esac
    else
      AGGREGATE_VERSION="$current_aggregate"
    fi
  else
    AGGREGATE_VERSION="$current_aggregate"
    if [ "$date" != "$current_date" ]; then
      echo "==> resume pending $current_aggregate before advancing to preview.$date"
    fi
  fi
  SELECTION="$TMP/selection.tsv"
  resolve_selection "$ROOT" "$AGGREGATE_VERSION" "$SELECTION"
  SANDBOXER_TAG="$(awk -F '\t' '$1 == "sandboxer" {print $2}' "$SELECTION")"
  RUNTIME_TAG="$(awk -F '\t' '$1 == "runtime" {print $2}' "$SELECTION")"
  echo "==> converge $AGGREGATE_VERSION"

  converge_until_deadline
}

converge_until_deadline() {
  local deadline
  deadline=$((SECONDS + ${PREVIEW_WAIT_SECONDS:-10800}))
  while true; do
    local rc=0
    converge_once || rc=$?
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    [ "$rc" -eq 10 ] || return "$rc"
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "==> preview remains pending; the recovery schedule will resume the same version"
      return 0
    fi
    sleep "${PREVIEW_POLL_SECONDS:-120}"
  done
}

command -v gh >/dev/null || release_fail "gh is required"
command -v jq >/dev/null || release_fail "jq is required"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
main "$@"
