#!/usr/bin/env bash
# Existing exact-request admission, shared by the artifact resolver.
set -euo pipefail
[ "$TRUSTED_WORKFLOW_REPOSITORY" = kuasar-sandbox/kuasar-sandbox ] \
  || { echo "Integration E2E implementation must come from kuasar-sandbox/kuasar-sandbox" >&2; exit 1; }
[[ "$TRUSTED_WORKFLOW_SHA" =~ ^[0-9a-f]{40}$ ]]
case "$INTEGRATION_MODE" in
  source)
    case "$GITHUB_EVENT_NAME" in
      pull_request_target) ;;
      pull_request)
        # Only the platform framework validates itself from an exact same-repo
        # merge commit. This route has no write token or inherited App secrets.
        [ "$GITHUB_REPOSITORY" = kuasar-sandbox/kuasar-sandbox ] \
          && [ "$CANDIDATE_REPOSITORY" = "$GITHUB_REPOSITORY" ] \
          && [ "$TRUSTED_WORKFLOW_SHA" = "$CANDIDATE_SHA" ] \
          && [ "$GITHUB_SHA" = "$CANDIDATE_SHA" ] \
          && [ "$COMPANION_CANDIDATES" = '[]' ] \
          && jq -e --arg repo "$GITHUB_REPOSITORY" \
            '.pull_request.head.repo.full_name == $repo' "$GITHUB_EVENT_PATH" >/dev/null \
          || { echo "framework CI requires the exact same-repository platform merge candidate" >&2; exit 1; }
        ;;
      push)
        [ "$GITHUB_REPOSITORY" = kuasar-sandbox/kuasar-sandbox ] \
          && [ "$CANDIDATE_REPOSITORY" = "$GITHUB_REPOSITORY" ] \
          && [ "$TRUSTED_WORKFLOW_SHA" = "$CANDIDATE_SHA" ] \
          && [ "$GITHUB_SHA" = "$CANDIDATE_SHA" ] \
          && [ "$COMPANION_CANDIDATES" = '[]' ] \
          && [ "$GITHUB_REF" = "refs/heads/ci/framework/pr-$CANDIDATE_PR/$CANDIDATE_SHA" ] \
          || { echo "framework push must name the exact platform PR and merge commit" >&2; exit 1; }
        ;;
      *) echo "source Integration E2E requires an admitted PR or framework candidate push" >&2; exit 1 ;;
    esac
    [[ "$CANDIDATE_REPOSITORY" =~ ^kuasar-sandbox/(accelerator|connector|guest-runtime|kuasar-sandbox|orchestrator|sandboxer)$ ]]
    for variable in CANDIDATE_SHA CANDIDATE_BASE_SHA CANDIDATE_HEAD_SHA; do
      value=${!variable}
      [[ "$value" =~ ^[0-9a-f]{40}$ ]] \
        || { echo "$variable must be a full lowercase commit SHA" >&2; exit 1; }
    done
    [[ "$CANDIDATE_BASE_REF" = main \
      || "$CANDIDATE_BASE_REF" =~ ^release/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x$ ]] \
      || { echo "CANDIDATE_BASE_REF must be main or release/vMAJOR.MINOR.x" >&2; exit 1; }
    [[ "$CANDIDATE_PR" =~ ^[1-9][0-9]*$ ]]
    jq -e --arg primary "$CANDIDATE_REPOSITORY" '
      type == "array"
      and length <= 5
      and ([.[].repository] | length == (unique | length))
      and all(.[];
        (keys == ["base_ref", "base_sha", "candidate_sha", "head_sha", "pull_request_number", "repository"])
        and (.repository | test("^kuasar-sandbox/(accelerator|connector|guest-runtime|kuasar-sandbox|orchestrator|sandboxer)$"))
        and .repository != $primary
        and (.pull_request_number | type == "number" and . >= 1 and . == floor)
        and (.candidate_sha | type == "string" and test("^[0-9a-f]{40}$"))
        and (.base_sha | type == "string" and test("^[0-9a-f]{40}$"))
        and (.base_ref == "main" or (.base_ref | test("^release/v(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.x$")))
        and (.head_sha | type == "string" and test("^[0-9a-f]{40}$"))
      )
    ' <<< "$COMPANION_CANDIDATES" >/dev/null \
      || { echo "companion_candidates is not a valid exact source set" >&2; exit 1; }
    if [ "$GITHUB_EVENT_NAME" = push ]; then
      jq -e --arg repository "$CANDIDATE_REPOSITORY" --arg ref "$GITHUB_REF" --arg sha "$CANDIDATE_SHA" '
        .repository.full_name == $repository and .repository.visibility == "public"
        and .ref == $ref and .after == $sha and .deleted == false
      ' "$GITHUB_EVENT_PATH" >/dev/null \
        || { echo "framework candidate inputs do not match the push event" >&2; exit 1; }
    else
    jq -e \
      --arg repository "$CANDIDATE_REPOSITORY" \
      --argjson number "$CANDIDATE_PR" \
      --arg base "$CANDIDATE_BASE_SHA" \
      --arg base_ref "$CANDIDATE_BASE_REF" \
      --arg head "$CANDIDATE_HEAD_SHA" '
        .repository.full_name == $repository
        and .pull_request.number == $number
        and .pull_request.state == "open"
        and .pull_request.draft == false
        and .pull_request.base.ref == $base_ref
        and .pull_request.base.repo.full_name == $repository
        and .pull_request.base.sha == $base
        and .pull_request.head.sha == $head
      ' "$GITHUB_EVENT_PATH" >/dev/null \
      || { echo "PR inputs do not match the admitted event" >&2; exit 1; }
    fi
    ;;
    exact-assets)
      [ "$GITHUB_REPOSITORY" = kuasar-sandbox/kuasar-sandbox ] \
        || { echo "only the actual platform caller may validate an aggregate stage" >&2; exit 1; }
    [ "$GITHUB_EVENT_NAME" = workflow_dispatch ] \
      || { echo "exact-assets Integration E2E requires workflow_dispatch" >&2; exit 1; }
    [[ "$RELEASE_VERSION" =~ ^release-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-preview\.[0-9]{8}(\.[1-9][0-9]*)?)?$ ]]
    [[ "$RELEASE_BUNDLE_ARTIFACT" =~ ^aggregate-stage-release-v[0-9]+\.[0-9]+\.[0-9]+(-preview\.[0-9]{8}(\.[1-9][0-9]*)?)?-[0-9]+$ ]]
    [[ "$PLATFORM_SOURCE_SHA" =~ ^[0-9a-f]{40}$ ]]
    ;;
  *) echo "mode must be source or exact-assets" >&2; exit 1 ;;
esac
