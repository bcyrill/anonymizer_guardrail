#!/usr/bin/env bash
# Push current branch and optionally cut a new vX.Y.Z tag.
#
# Triggers the GitHub Actions workflows in .github/workflows/:
#   - publish-image.yml  (builds & pushes the container to ghcr.io)
#   - release.yml        (creates a GitHub Release on tag push)

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
say()  { printf '%s\n' "$*"; }
warn() { printf '%s%s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_rst" >&2; }
ok()   { printf '%s%s%s\n' "$c_grn" "$*" "$c_rst"; }

confirm() {
  local prompt="$1" default="${2:-n}" reply
  local hint='[y/N]'; [[ "$default" == "y" ]] && hint='[Y/n]'
  read -r -p "$prompt $hint " reply || true
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy]$ ]]
}

branch="$(git rev-parse --abbrev-ref HEAD)"
remote="$(git config "branch.${branch}.remote" || echo origin)"

say "Branch:  ${c_grn}${branch}${c_rst}"
say "Remote:  ${c_grn}${remote}${c_rst}"

# 1. Working tree must be clean.
if ! git diff --quiet || ! git diff --cached --quiet; then
  err "Working tree has uncommitted changes. Commit or stash first."
  git status --short
  exit 1
fi

# 2. Show what's about to be pushed.
git fetch --quiet "$remote" "$branch" 2>/dev/null || true
ahead="$(git rev-list --count "${remote}/${branch}..HEAD" 2>/dev/null || echo "?")"
behind="$(git rev-list --count "HEAD..${remote}/${branch}" 2>/dev/null || echo 0)"

if [[ "$behind" != "0" && "$behind" != "?" ]]; then
  warn "Local is behind ${remote}/${branch} by ${behind} commit(s). Pull/rebase first."
  exit 1
fi

if [[ "$ahead" == "0" ]]; then
  say "${c_dim}No new commits to push.${c_rst}"
else
  say ""
  say "Commits to push (${ahead}):"
  git --no-pager log --oneline "${remote}/${branch}..HEAD" 2>/dev/null \
    || git --no-pager log --oneline -n "${ahead}" HEAD
  say ""
  if confirm "Push ${branch} to ${remote}?" y; then
    git push "$remote" "$branch"
    ok "Pushed ${branch}."
  else
    warn "Skipped push."
  fi
fi

# 3. Tagging.
say ""
if ! confirm "Create a new release tag?" n; then
  ok "Done."
  exit 0
fi

latest_tag="$(git tag --list 'v*.*.*' --sort=-v:refname | head -n1 || true)"
if [[ -z "$latest_tag" ]]; then
  say "No existing v*.*.* tag found."
  default_tag="v0.1.0"
else
  say "Latest tag: ${c_grn}${latest_tag}${c_rst}"
  IFS='.' read -r maj min pat <<<"${latest_tag#v}"
  pat_next="v${maj}.${min}.$((pat + 1))"
  min_next="v${maj}.$((min + 1)).0"
  maj_next="v$((maj + 1)).0.0"
  say ""
  say "  1) patch  → ${pat_next}"
  say "  2) minor  → ${min_next}"
  say "  3) major  → ${maj_next}"
  say "  4) custom"
  read -r -p "Choose [1-4, default 1]: " choice || true
  case "${choice:-1}" in
    1) default_tag="$pat_next" ;;
    2) default_tag="$min_next" ;;
    3) default_tag="$maj_next" ;;
    4) default_tag="" ;;
    *) err "Invalid choice."; exit 1 ;;
  esac
fi

if [[ -z "$default_tag" ]]; then
  read -r -p "New tag (e.g. v1.2.3): " new_tag
else
  read -r -p "New tag [${default_tag}]: " new_tag
  new_tag="${new_tag:-$default_tag}"
fi

if [[ ! "$new_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  err "Tag '${new_tag}' is not a valid vX.Y.Z[-pre] form."
  exit 1
fi

if git rev-parse "$new_tag" >/dev/null 2>&1; then
  err "Tag ${new_tag} already exists."
  exit 1
fi

read -r -p "Annotation message [Release ${new_tag}]: " msg || true
msg="${msg:-Release ${new_tag}}"

say ""
say "About to:"
say "  git tag -a ${c_grn}${new_tag}${c_rst} -m \"${msg}\""
say "  git push ${remote} ${c_grn}${new_tag}${c_rst}"
if ! confirm "Proceed?" y; then
  warn "Aborted."
  exit 0
fi

git tag -a "$new_tag" -m "$msg"
git push "$remote" "$new_tag"
ok "Tag ${new_tag} pushed. GitHub Actions will build the image and create the release."
say "${c_dim}Watch: https://github.com/$(git config --get remote."${remote}".url | sed -E 's#.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')/actions${c_rst}"