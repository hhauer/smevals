#!/usr/bin/env bash
# Build the v2 recording suite: copies of code-review and
# pelican-riding-a-bicycle (runs included, so results/compare/gallery
# beats have real data) in a throwaway dir the movie may freely write to.
# Usage: setup_scratch_suite_v2.sh <smevals-repo> <dest-dir>
set -euo pipefail
repo="${1:?usage: setup_scratch_suite_v2.sh <smevals-repo> <dest-dir>}"
dest="${2:?usage: setup_scratch_suite_v2.sh <smevals-repo> <dest-dir>}"
[[ -d "$repo/examples/code-review" ]] || { echo "no code-review under $repo/examples" >&2; exit 1; }
[[ -e "$dest" ]] && { echo "$dest already exists - refusing to overwrite" >&2; exit 1; }
mkdir -p "$dest"
for ev in code-review pelican-riding-a-bicycle; do
  cp -R "$repo/examples/$ev" "$dest/$ev"
done
# code-review's graders reference ../checkers - bring them along
[[ -d "$repo/examples/code-review/../checkers" ]] && cp -R "$repo/examples/checkers" "$dest/checkers" 2>/dev/null || true
echo "scratch suite ready at $dest"
ls "$dest"
