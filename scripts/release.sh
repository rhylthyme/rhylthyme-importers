#!/usr/bin/env bash
# Release rhylthyme-importers to PyPI, then open the server PR that requires it.
#
#   make release                  # next patch version
#   make release VERSION=0.5.0    # a specific version
#   DRY_RUN=1 make release        # stop before committing anything
#
# Run on a clean main. Nothing is committed until the tests, build and package
# check pass, and it asks once before publishing. YES=1 skips that question.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

PACKAGE=rhylthyme-importers
SERVER_REPO=${SERVER_REPO:-../rhylthyme-server}
die() { echo "release: $*" >&2; exit 1; }

if [ -n "${PYTHON:-}" ]; then PY=$PYTHON
elif [ -n "${VIRTUAL_ENV:-}" ]; then PY=$VIRTUAL_ENV/bin/python
elif [ -x ../.venv/bin/python ]; then PY=$PWD/../.venv/bin/python
else PY=python3
fi
TWINE=${TWINE:-$PY -m twine}

# --- Preflight: the release has to be exactly what's on origin/main.
[ "$(git branch --show-current)" = main ] || die "switch to main first"
[ -z "$(git status --porcelain)" ] || die "working tree isn't clean"
git fetch origin --tags --quiet
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
    || die "main isn't level with origin/main (git pull, or push your commits)"
"$PY" -m build --version >/dev/null || die "the build package isn't installed in $PY"
$TWINE --version >/dev/null || die "twine isn't installed in $PY"

current=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)
[ -n "$current" ] || die "no version found in pyproject.toml"
next=${1:-${VERSION:-}}
if [ -z "$next" ]; then
    case "$current" in
        *.*.*) next=${current%.*}.$((${current##*.} + 1)) ;;
        *) die "can't work out the next version from $current; pass VERSION=" ;;
    esac
fi
[[ "$next" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "not a version: $next"
[ "$next" != "$current" ] || die "pyproject.toml is already at $next"
! git rev-parse -q --verify "refs/tags/v$next" >/dev/null || die "tag v$next already exists"
status=$(curl -s -o /dev/null -w '%{http_code}' "https://pypi.org/pypi/$PACKAGE/$next/json")
[ "$status" != 200 ] || die "$next is already on PyPI"

echo "==> Releasing $PACKAGE $current -> $next"

# Until the commit is made, a failure or dry run puts the version back.
committed=
restore() { [ -n "$committed" ] || git checkout -q -- pyproject.toml; }
trap restore EXIT

# --- Everything that can fail cheaply comes before the commit.
echo "==> Tests"
"$PY" -m pytest tests -q
"$PY" - "$next" <<'EOF'
import re, sys
text = open("pyproject.toml").read()
text = re.sub(r'^version = ".*"$', f'version = "{sys.argv[1]}"', text, count=1, flags=re.M)
open("pyproject.toml", "w").write(text)
EOF
echo "==> Build"
rm -rf dist
"$PY" -m build >/dev/null
artifacts=(dist/rhylthyme_importers-"$next"*)
[ "${#artifacts[@]}" = 2 ] || die "expected a wheel and an sdist, got: ${artifacts[*]}"
$TWINE check "${artifacts[@]}"

if [ -n "${DRY_RUN:-}" ]; then
    echo "==> Dry run passed: $next would be published. Nothing was committed."
    exit 0
fi

# --- PyPI versions can't be replaced, so this is the one question it asks.
if [ -z "${YES:-}" ]; then
    read -r -p "Publish $PACKAGE $next to PyPI? This can't be undone. [y/N] " answer
    [ "$answer" = y ] || [ "$answer" = Y ] || die "cancelled; nothing was committed"
fi

git add pyproject.toml
git commit -q -m "Release $next"
committed=1
git tag "v$next"
git push -q --atomic origin main "v$next"

echo "==> Upload"
$TWINE upload "${artifacts[@]}" || die "upload failed. main and v$next are pushed, so retry with:
  $TWINE upload ${artifacts[*]}"
echo "==> Published https://pypi.org/project/$PACKAGE/$next/"

# --- The server requires the new version. A failure here doesn't undo the
# release, so it only warns.
[ -e "$SERVER_REPO/.git" ] || { echo "==> No server checkout at $SERVER_REPO; update its requirement by hand."; exit 0; }
echo "==> Server PR"
worktree=$(mktemp -d)/server
set +e
(
    set -e
    branch="importers-$next"
    git -C "$SERVER_REPO" fetch origin --quiet
    git -C "$SERVER_REPO" worktree add -q -b "$branch" "$worktree" origin/main
    "$PY" - "$next" "$worktree" <<'EOF'
import re, sys
for name in ("pyproject.toml", "requirements.txt"):
    path = f"{sys.argv[2]}/{name}"
    text = open(path).read()
    open(path, "w").write(re.sub(r"rhylthyme-importers>=[0-9.]+", f"rhylthyme-importers>={sys.argv[1]}", text))
EOF
    git -C "$worktree" commit -q -am "Require rhylthyme-importers $next"
    git -C "$worktree" push -q -u origin "$branch"
    gh pr create --repo rhylthyme/rhylthyme-server --base main --head "$branch" \
        --title "Require rhylthyme-importers $next" \
        --body "Picks up the $next release: https://pypi.org/project/$PACKAGE/$next/"
)
server_status=$?
set -e
git -C "$SERVER_REPO" worktree remove --force "$worktree" 2>/dev/null
[ "$server_status" = 0 ] || echo "release: $next is published, but the server PR failed. Set rhylthyme-importers>=$next in its pyproject.toml and requirements.txt by hand." >&2
