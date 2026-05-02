#!/usr/bin/env bash
# Push current branch and optionally cut a new vX.Y.Z tag.
#
# Pushing the branch does NOT trigger any GitHub Actions workflow on
# its own — image builds and GitHub Releases are tag-driven only, so
# day-to-day commits don't churn the container registry. Local testing
# via scripts/build-image.sh covers the iteration loop.
#
# Pushing a tag triggers the workflows in .github/workflows/:
#   - publish-image.yml                  (guardrail image → ghcr.io)
#   - publish-pf-service-image.yml       (privacy-filter-service → ghcr.io)
#   - publish-gliner-service-image.yml   (gliner-pii-service → ghcr.io)
#   - release.yml                        (GitHub Release; canonical tag only)
#
# After the canonical vX.Y.Z tag, the script optionally also pushes
# flavour-variant tags pointing at the same commit:
#   vX.Y.Z+pf             → publishes the guardrail pf image
#   vX.Y.Z+pf-service     → publishes the standalone privacy-filter-service
#   vX.Y.Z+gliner-service → publishes the standalone gliner-pii-service
# Each one triggers its matching workflow separately. release.yml only
# creates a GitHub Release for the canonical tag (variants reuse it).
#
# Baked variants (`+pf-baked`, `+pf-service-baked`, `+gliner-service-baked`)
# are NOT published from CI — the images are multi-GB. Build them
# locally with `scripts/build-image.sh -t pf-baked` (or
# `-t pf-service-baked` / `-t gliner-service-baked`).

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

# Exclude flavour-variant tags (anything with a `+` suffix) — they
# share the version of their canonical sibling and break the bump
# arithmetic below (split on `.` would leave the `+pf` suffix in
# `pat`, then `$((pat + 1))` blows up under `set -u`).
latest_tag="$(git tag --list 'v*.*.*' --sort=-v:refname | grep -v '+' | head -n1 || true)"
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
ok "Tag ${new_tag} pushed. GitHub Actions will build the slim image and create the release."

# ── Flavour variants ─────────────────────────────────────────────────────────
# Three independent axes, each prompted separately so an operator can
# decide per-axis instead of navigating a combined menu.
#
# Axis 1 — guardrail privacy-filter flavour (publish-image.yml):
#   vX.Y.Z+pf → guardrail image with privacy-filter built-in
#
# Axis 2 — standalone privacy-filter-service (publish-pf-service-image.yml):
#   vX.Y.Z+pf-service → separate ghcr package: privacy-filter-service
#
# Axis 3 — standalone gliner-pii-service (publish-gliner-service-image.yml):
#   vX.Y.Z+gliner-service → separate ghcr package: gliner-pii-service
#
# Baked variants of any axis are intentionally absent — they're
# local-build only (`scripts/build-image.sh -t pf-baked` /
# `-t pf-service-baked` / `-t gliner-service-baked`).
variants=()

say ""
say "Also publish the guardrail privacy-filter image?"
say ""
say "  ${c_grn}1)${c_rst} no, slim only"
say "  ${c_grn}2)${c_rst} +pf — guardrail with privacy-filter built-in"
read -r -p "Choose [1-2, default 1]: " gv_choice || true
case "${gv_choice:-1}" in
  1) ;;
  2) variants+=("pf") ;;
  *) err "Invalid choice."; exit 1 ;;
esac

say ""
say "Also publish the standalone privacy-filter-service image?"
say "(separate ghcr package; pair with the slim guardrail and PRIVACY_FILTER_URL)"
say ""
say "  ${c_grn}1)${c_rst} no"
say "  ${c_grn}2)${c_rst} +pf-service"
read -r -p "Choose [1-2, default 1]: " sv_choice || true
case "${sv_choice:-1}" in
  1) ;;
  2) variants+=("pf-service") ;;
  *) err "Invalid choice."; exit 1 ;;
esac

say ""
say "Also publish the standalone gliner-pii-service image?"
say "(separate ghcr package; pair with the slim guardrail and GLINER_PII_URL)"
say ""
say "  ${c_grn}1)${c_rst} no"
say "  ${c_grn}2)${c_rst} +gliner-service"
read -r -p "Choose [1-2, default 1]: " gs_choice || true
case "${gs_choice:-1}" in
  1) ;;
  2) variants+=("gliner-service") ;;
  *) err "Invalid choice."; exit 1 ;;
esac

for v in "${variants[@]}"; do
  variant_tag="${new_tag}+${v}"
  if git rev-parse "$variant_tag" >/dev/null 2>&1; then
    warn "Tag ${variant_tag} already exists — skipping."
    continue
  fi
  git tag -a "$variant_tag" -m "${msg} (${v})"
  git push "$remote" "$variant_tag"
  ok "Tag ${variant_tag} pushed."
done

say "${c_dim}Watch: https://github.com/$(git config --get remote."${remote}".url | sed -E 's#.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')/actions${c_rst}"