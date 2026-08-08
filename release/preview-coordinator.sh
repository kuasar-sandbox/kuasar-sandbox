#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=release/lib.sh
source "$ROOT/release/lib.sh"

readonly INFRA_FAILURE_RE='(The self-hosted runner.*lost communication|runner.*(offline|lost)|No space left on device|Connection (timed out|reset)|TLS handshake timeout|Temporary failure|Could not resolve host|unexpected EOF|HTTP (429|500|502|503|504)|failed to download|rate limit)'
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

release_exists() {
  local repository="$1" tag="$2" output="$3"
  if api_optional "repos/$repository/releases/tags/$tag" "$output"; then
    jq -e --arg tag "$tag" '.tag_name == $tag and .draft == false' "$output" >/dev/null \
      || release_fail "$repository $tag exists but is not a published release"
    return 0
  fi
  local rc=$?
  [ "$rc" -eq 4 ] || exit "$rc"
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

discover_pending_date() {
  local stable_component="$1" today="$2" runs="$TMP/runs.json" dates="$TMP/dates"
  gh api --paginate --slurp \
    "repos/kuasar-sandbox/accelerator/actions/workflows/release.yml/runs?event=workflow_dispatch&per_page=100" \
    > "$runs"
  jq -r --arg prefix "Release $stable_component-preview." '
      .[].workflow_runs[].display_title
      | select(startswith($prefix))
      | sub("^Release .*\\."; "")
      | select(test("^[0-9]{8}$"))
    ' "$runs" | LC_ALL=C sort -u > "$dates"
  local date state
  while IFS= read -r date; do
    [ "$date" -le "$today" ] || continue
    if ! release_exists kuasar-sandbox/platform "release-$stable_component-preview.$date" "$TMP/pending-release.json"; then
      printf '%s\n' "$date"
      return 0
    fi
  done < "$dates"
  printf '%s\n' "$today"
}

formal_release_started() {
  local stable_aggregate="$1" selection="$TMP/stable-selection.tsv" unit tag repository
  resolve_selection "$ROOT" "$stable_aggregate" "$selection"
  if release_exists kuasar-sandbox/platform "$stable_aggregate" "$TMP/stable-platform.json"; then
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
  if release_exists kuasar-sandbox/platform "$AGGREGATE_VERSION" "$platform_state"; then
    echo "==> aggregate $AGGREGATE_VERSION is published"
    return 0
  fi
  rc=0
  ensure_workflow kuasar-sandbox/platform aggregate-release.yml \
    "Aggregate $AGGREGATE_VERSION" -f "version=$AGGREGATE_VERSION" || rc=$?
  if [ "$rc" -ne 0 ]; then
    [ "$rc" -eq 10 ] || return "$rc"
    return 10
  fi
}

main() {
  local requested_date="${1:-}" stable_aggregate stable_component today date deadline
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
  if formal_release_started "$stable_aggregate"; then
    return 0
  fi
  today="$(TZ=Asia/Shanghai date +%Y%m%d)"
  if [ -n "$requested_date" ]; then
    [[ "$requested_date" =~ ^[0-9]{8}$ ]] || release_fail "date must match YYYYMMDD"
    date="$requested_date"
  else
    date="$(discover_pending_date "$stable_component" "$today")"
  fi
  AGGREGATE_VERSION="release-$stable_component-preview.$date"
  validate_aggregate_version "$AGGREGATE_VERSION"
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
