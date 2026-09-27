#!/usr/bin/env bash
# Existing exact-request admission, shared by the artifact resolver.
set -euo pipefail
source_api_get() {
  curl --fail --show-error --silent \
    --retry 4 --retry-all-errors --connect-timeout 10 --max-time 60 \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com$1"
}

caller_api_get() {
  curl --fail --show-error --silent \
    --retry 4 --retry-all-errors --connect-timeout 10 --max-time 60 \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $CALLER_TOKEN" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com$1"
}

validate_primary() {
  local response pr
  response=$(source_api_get "/repos/$CANDIDATE_REPOSITORY/git/commits/$CANDIDATE_SHA")
  pr=$(caller_api_get "/repos/$CANDIDATE_REPOSITORY/pulls/$CANDIDATE_PR")
  jq -e \
    --arg candidate "$CANDIDATE_SHA" \
    --arg base "$CANDIDATE_BASE_SHA" \
    --arg head "$CANDIDATE_HEAD_SHA" '
      .sha == $candidate
      and (.parents | length) == 2
      and .parents[0].sha == $base
      and .parents[1].sha == $head
    ' <<< "$response" >/dev/null \
    && jq -e \
      --arg repository "$CANDIDATE_REPOSITORY" \
      --argjson number "$CANDIDATE_PR" \
      --arg candidate "$CANDIDATE_SHA" \
      --arg base "$CANDIDATE_BASE_SHA" \
      --arg base_ref "$CANDIDATE_BASE_REF" \
      --arg head "$CANDIDATE_HEAD_SHA" '
        .number == $number
        and .state == "open"
        and .draft == false
        and .base.ref == $base_ref
        and .base.repo.full_name == $repository
        and .base.sha == $base
        and .head.sha == $head
        and .merge_commit_sha == $candidate
      ' <<< "$pr" >/dev/null
}

validate_companion() {
  local record=$1 repository number candidate base base_ref head
  local merge_ref head_ref commit pr base_branch branches snapshot
  repository=$(jq -r '.repository' <<< "$record")
  number=$(jq -r '.pull_request_number' <<< "$record")
  candidate=$(jq -r '.candidate_sha' <<< "$record")
  base=$(jq -r '.base_sha' <<< "$record")
  base_ref=$(jq -r '.base_ref' <<< "$record")
  head=$(jq -r '.head_sha' <<< "$record")
  merge_ref=$(source_api_get "/repos/$repository/git/ref/pull/$number/merge") \
    || return 1
  head_ref=$(source_api_get "/repos/$repository/git/ref/pull/$number/head") \
    || return 1
  commit=$(source_api_get "/repos/$repository/git/commits/$candidate") \
    || return 1
  pr=$(source_api_get "/repos/$repository/pulls/$number") \
    || return 1
  base_branch=$(source_api_get "/repos/$repository/git/ref/heads/$base_ref") \
    || return 1
  branches=$(source_api_get "/repos/$repository/commits/$head/branches-where-head") \
    || return 1
  snapshot=$(jq -cn \
    --argjson merge "$merge_ref" \
    --argjson head_ref "$head_ref" \
    --argjson commit "$commit" \
    --argjson pr "$pr" \
    --argjson base_branch "$base_branch" \
    --argjson branches "$branches" \
    '{merge: $merge, head_ref: $head_ref, commit: $commit,
      pr: $pr, base_branch: $base_branch, branches: $branches}')
  jq -e \
    --arg repository "$repository" \
    --argjson number "$number" \
    --arg merge_ref "refs/pull/$number/merge" \
    --arg head_ref "refs/pull/$number/head" \
    --arg candidate "$candidate" \
    --arg base "$base" \
    --arg base_ref "$base_ref" \
    --arg head "$head" '
      .merge.ref == $merge_ref
      and .merge.object.sha == $candidate
      and .head_ref.ref == $head_ref
      and .head_ref.object.sha == $head
      and .commit.sha == $candidate
      and (.commit.parents | length) == 2
      and .commit.parents[0].sha == $base
      and .commit.parents[1].sha == $head
      and .pr.number == $number
      and .pr.state == "open"
      and .pr.draft == false
      and .pr.base.ref == $base_ref
      and .pr.base.repo.full_name == $repository
      and .pr.base.sha == $base
      and .pr.head.sha == $head
      and .base_branch.object.sha == $base
      and (.branches | type == "array" and length > 0)
    ' <<< "$snapshot" >/dev/null
}

validate_primary \
  || { echo "candidate no longer matches the exact open pull request" >&2; exit 1; }
while IFS= read -r companion; do
  validate_companion "$companion" \
    || { echo "companion no longer matches the exact open pull request" >&2; exit 1; }
done < <(jq -c '.[]' <<< "$COMPANION_CANDIDATES")
