#!/usr/bin/env bash
set -euo pipefail

if [ -x /usr/bin/bash ]; then
  BASH_BIN=/usr/bin/bash
elif [ -x /bin/bash ]; then
  BASH_BIN=/bin/bash
else
  echo "release validation requires Bash at /usr/bin/bash or /bin/bash" >&2
  exit 2
fi

GIT_BIN="/usr/bin/git"
if [ ! -x "$GIT_BIN" ]; then
  echo "release validation requires Git at $GIT_BIN" >&2
  exit 2
fi
git() {
  "$GIT_BIN" "$@"
}
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
unset GIT_CEILING_DIRECTORIES GIT_NAMESPACE GIT_REPLACE_REF_BASE
unset GIT_EXEC_PATH GIT_TEMPLATE_DIR

ROOT="${GO_RELEASE_ROOT:?release launcher did not provide GO_RELEASE_ROOT}"
VERSION=""
EXISTING_MODE=0
ALLOW_CANDIDATE=0
ALLOW_LOCAL_ORIGIN=0
EXPLICIT_PROJECT_TEMPLATE="${GO_PROJECT_TEMPLATE_SET:-0}"

if [ "${1:-}" != "" ] && [[ "${1:-}" != --* ]]; then
  VERSION="$1"
  shift
fi
while [ "$#" -gt 0 ]; do
  case "$1" in
    --validate-existing) EXISTING_MODE=1 ;;
    --allow-candidate) ALLOW_CANDIDATE=1 ;;
    --allow-local-origin) ALLOW_LOCAL_ORIGIN=1 ;;
    *)
      echo "unknown release validation option: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [ "$EXISTING_MODE" = "1" ] && [ "$EXPLICIT_PROJECT_TEMPLATE" = "1" ]; then
  echo "--validate-existing refuses GO_PROJECT_TEMPLATE; validate template compatibility against the current stack separately" >&2
  exit 2
fi

for legacy_control in GO_RELEASE_VALIDATE_EXISTING GO_RELEASE_ALLOW_CANDIDATE GO_RELEASE_ALLOW_LOCAL_ORIGIN GO_RELEASE_SKIP_TESTS; do
  if [ -n "${!legacy_control:-}" ]; then
    echo "$legacy_control is no longer accepted; use explicit release-check.sh flags" >&2
    exit 2
  fi
done

if [ -z "$VERSION" ]; then
  VERSION="$(sed -n 's/^version = "\([0-9][0-9.]*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)"
fi

if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "release version must be X.Y.Z" >&2
  exit 2
fi

python3 - "$ROOT" "$VERSION" <<'PY'
import json
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
pairings = json.loads((root / 'release-pairings.json').read_text(encoding='utf-8'))
if pairings.get('current_stack_ref') != 'v' + expected:
    raise SystemExit('release pairing current ref differs from release version')
constants = (root / "go_workflow" / "constants.py").read_text(encoding="utf-8")
pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
project = json.loads((root / ".go" / "project.json").read_text(encoding="utf-8"))

values = {
    "runtime STACK_VERSION": re.search(r'^STACK_VERSION = "([^"]+)"', constants, re.M).group(1),
    "pyproject version": re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1),
    ".go required_stack_version": project.get("required_stack_version"),
}
for label, value in values.items():
    if value != expected:
        raise SystemExit(f"{label} is {value!r}, expected {expected!r}")
if project.get("stack_ref") != f"v{expected}":
    raise SystemExit(f".go stack_ref is {project.get('stack_ref')!r}, expected 'v{expected}'")
PY

echo "release preflight: v$VERSION"
SOURCE_ROOT="$ROOT"
ARCHIVE_WORK=""
cleanup() {
  rm -rf "${ARCHIVE_WORK:-}"
}
trap cleanup EXIT

release_origin_url() {
  local raw_url
  raw_url="$(sanitized_git -C "$ROOT" config --local --get remote.origin.url 2>/dev/null || true)"
  if [ -z "$raw_url" ]; then
    echo "release validation requires an origin remote" >&2
    return 1
  fi
  normalized_transport="$(printf '%s' "$raw_url" | tr '[:upper:]' '[:lower:]')"
  case "$normalized_transport" in
    git+*|*::*)
      echo "release validation refuses remote-helper origin $raw_url" >&2
      return 1
      ;;
  esac
  if python3 - "$ROOT" "$raw_url" <<'PY'
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
from go_workflow.runtime_identity import _official_repository_url
raise SystemExit(0 if _official_repository_url(sys.argv[2]) else 1)
PY
  then
    case "$normalized_transport" in
      https://github.com/viggomeesters/go-workflow-stack|https://github.com/viggomeesters/go-workflow-stack.git)
        printf '%s\n' "https://github.com/viggomeesters/go-workflow-stack.git"
        return 0
        ;;
      *)
        echo "release hydration requires the official HTTPS origin; SSH origins are not accepted" >&2
        return 1
        ;;
    esac
  fi
  if [ "$ALLOW_LOCAL_ORIGIN" != "1" ]; then
    echo "release hydration refuses non-official origin $raw_url" >&2
    return 1
  fi
  case "$raw_url" in
    file://*|/*)
      printf '%s\n' "$raw_url"
      ;;
    *://*|*::*|git@*)
      echo "local release origin override only accepts filesystem remotes" >&2
      return 1
      ;;
    *)
      python3 - "$ROOT" "$raw_url" <<'PY'
from pathlib import Path
import sys
print((Path(sys.argv[1]) / sys.argv[2]).resolve())
PY
      ;;
  esac
}

sanitized_git() (
  unset BASH_ENV ENV
  unset GIT_CONFIG
  unset GIT_CONFIG_COUNT
  unset GIT_CONFIG_PARAMETERS
  unset GIT_SSH
  unset GIT_SSH_COMMAND
  unset GIT_SSH_VARIANT
  unset GIT_PROXY_COMMAND
  unset GIT_EXEC_PATH
  unset GIT_TEMPLATE_DIR
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
  unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  unset GIT_CEILING_DIRECTORIES GIT_NAMESPACE GIT_REPLACE_REF_BASE
  unset GIT_SSL_NO_VERIFY
  unset GIT_SSL_CAINFO
  unset GIT_SSL_CAPATH
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
  unset http_proxy https_proxy all_proxy no_proxy
  unset CURL_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR
  export GIT_CONFIG_GLOBAL=/dev/null
  export GIT_CONFIG_SYSTEM=/dev/null
  export GIT_CONFIG_NOSYSTEM=1
  export GIT_OPTIONAL_LOCKS=0
  export GIT_NO_REPLACE_OBJECTS=1
  git --no-replace-objects "$@"
)

isolated_status() (
  repo="$1"
  expected_head="$2"
  status_git="$(mktemp -d /tmp/go-release-status.XXXXXX)"
  trap '/bin/rm -rf "$status_git"' EXIT

  if ! common_git_dir="$(sanitized_git -C "$repo" rev-parse --path-format=absolute --git-common-dir)"; then
    exit 1
  fi
  if ! source_index="$(sanitized_git -C "$repo" rev-parse --path-format=absolute --git-path index)"; then
    exit 1
  fi
  if ! object_format="$(sanitized_git -C "$repo" rev-parse --show-object-format)"; then
    exit 1
  fi
  if [ ! -f "$source_index" ]; then
    echo "release validation requires a readable repository index at $source_index" >&2
    exit 1
  fi

  if ! sanitized_git init --bare -q --object-format="$object_format" "$status_git"; then
    exit 1
  fi
  printf '%s\n' "$common_git_dir/objects" >"$status_git/objects/info/alternates"
  /bin/cp -- "$source_index" "$status_git/index"
  printf '%s\n' "$expected_head" >"$status_git/HEAD"
  if ! index_tree="$(sanitized_git --git-dir="$status_git" write-tree)"; then
    exit 1
  fi
  if ! expected_tree="$(sanitized_git --git-dir="$status_git" rev-parse "$expected_head^{tree}")"; then
    exit 1
  fi
  if [ "$index_tree" != "$expected_tree" ]; then
    printf '%s\n' "caller index differs from expected HEAD"
  fi
  if ! sanitized_git --git-dir="$status_git" read-tree "$expected_head"; then
    exit 1
  fi
  sanitized_git \
    --git-dir="$status_git" \
    --work-tree="$repo" \
    -c core.bare=false \
    -c core.worktree="$repo" \
    -c core.fsmonitor=false \
    -c status.showUntrackedFiles=all \
    status --porcelain=v1 --untracked-files=all
)

sanitized_gate() (
  unset BASH_ENV ENV
  unset GIT_CONFIG
  unset GIT_CONFIG_COUNT
  unset GIT_CONFIG_PARAMETERS
  unset GIT_SSH
  unset GIT_SSH_COMMAND
  unset GIT_SSH_VARIANT
  unset GIT_PROXY_COMMAND
  unset GIT_EXEC_PATH
  unset GIT_TEMPLATE_DIR
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
  unset GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES
  unset GIT_CEILING_DIRECTORIES GIT_NAMESPACE GIT_REPLACE_REF_BASE
  unset GIT_SSL_NO_VERIFY
  unset GIT_SSL_CAINFO
  unset GIT_SSL_CAPATH
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
  unset http_proxy https_proxy all_proxy no_proxy
  unset CURL_CA_BUNDLE SSL_CERT_FILE SSL_CERT_DIR
  /usr/bin/env -i \
    HOME="${HOME:-/tmp}" \
    PATH="/usr/local/bin:/usr/bin:/bin" \
    LANG="${LANG:-C.UTF-8}" \
    TMPDIR="${TMPDIR:-/tmp}" \
    PYTHONNOUSERSITE=1 \
    GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null \
    GIT_CONFIG_NOSYSTEM=1 \
    GIT_OPTIONAL_LOCKS=0 \
    GIT_NO_REPLACE_OBJECTS=1 \
    "$@"
)

release_git() (
  export GIT_ALLOW_PROTOCOL="$release_protocol"
  sanitized_git "$@"
)

existing_mode="$EXISTING_MODE"
local_tag_present=0
if sanitized_git -C "$ROOT" rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
  local_tag_present=1
fi

if [ "$existing_mode" = "1" ] || [ "$local_tag_present" = "1" ]; then
  head_commit="$(sanitized_git -C "$ROOT" rev-parse HEAD)"
  if ! root_status="$(isolated_status "$ROOT" "$head_commit")"; then
    echo "release validation could not inspect the caller worktree" >&2
    exit 1
  fi
  if [ -n "$root_status" ]; then
    echo "release worktree must be clean when validating tag v$VERSION" >&2
    exit 1
  fi
  ARCHIVE_WORK="$(mktemp -d)"
  SOURCE_ROOT="$ARCHIVE_WORK/go-workflow-stack"
  ARCHIVE_REPO="$ROOT"

  if [ "$existing_mode" = "1" ]; then
    if ! release_remote="$(release_origin_url)"; then
      exit 1
    fi
    normalized_release_remote="$(printf '%s' "$release_remote" | tr '[:upper:]' '[:lower:]')"
    case "$normalized_release_remote" in
      https://*) release_protocol="https" ;;
      file://*|/*) release_protocol="file" ;;
      *)
        echo "release validation could not determine a safe Git protocol for $release_remote" >&2
        exit 1
        ;;
    esac
    release_git clone -q --no-checkout --no-local "$release_remote" "$SOURCE_ROOT"
    release_git -C "$SOURCE_ROOT" fetch -q --tags origin
    ARCHIVE_REPO="$SOURCE_ROOT"

    remote_tag_type="$(sanitized_git -C "$SOURCE_ROOT" cat-file -t "refs/tags/v$VERSION" 2>/dev/null || true)"
    if [ "$remote_tag_type" != "tag" ]; then
      echo "origin tag v$VERSION must be annotated, found ${remote_tag_type:-missing}" >&2
      exit 1
    fi
    tag_object="$(sanitized_git -C "$SOURCE_ROOT" rev-parse "refs/tags/v$VERSION")"
    tag_commit="$(sanitized_git -C "$SOURCE_ROOT" rev-list -n 1 "refs/tags/v$VERSION")"

    if [ "$local_tag_present" = "1" ]; then
      local_tag_type="$(sanitized_git -C "$ROOT" cat-file -t "refs/tags/v$VERSION")"
      if [ "$local_tag_type" != "tag" ]; then
        echo "tag v$VERSION must be annotated, found $local_tag_type" >&2
        exit 1
      fi
      local_tag_object="$(sanitized_git -C "$ROOT" rev-parse "refs/tags/v$VERSION")"
      local_tag_commit="$(sanitized_git -C "$ROOT" rev-list -n 1 "refs/tags/v$VERSION")"
      if [ "$tag_object" != "$local_tag_object" ]; then
        echo "origin tag object v$VERSION does not match local annotated tag $local_tag_object" >&2
        exit 1
      fi
      if [ "$tag_commit" != "$local_tag_commit" ]; then
        echo "origin tag v$VERSION does not resolve to local tag commit $local_tag_commit" >&2
        exit 1
      fi
    fi
    if ! sanitized_git -C "$SOURCE_ROOT" cat-file -e "$head_commit^{commit}" 2>/dev/null; then
      echo "HEAD $head_commit is not present in origin" >&2
      exit 1
    fi
    if [ -z "$(sanitized_git -C "$SOURCE_ROOT" for-each-ref --format='%(refname)' --contains "$head_commit" refs/remotes/origin/)" ]; then
      echo "HEAD $head_commit is not reachable from an origin branch" >&2
      exit 1
    fi
    if ! sanitized_git -C "$SOURCE_ROOT" merge-base --is-ancestor "$tag_commit" "$head_commit"; then
      echo "tag v$VERSION is not an ancestor of HEAD; refusing release validation" >&2
      exit 1
    fi
  else
    tag_type="$(sanitized_git -C "$ROOT" cat-file -t "refs/tags/v$VERSION")"
    if [ "$tag_type" != "tag" ]; then
      echo "tag v$VERSION must be annotated, found $tag_type" >&2
      exit 1
    fi
    tag_object="$(sanitized_git -C "$ROOT" rev-parse "refs/tags/v$VERSION")"
    tag_commit="$(sanitized_git -C "$ROOT" rev-list -n 1 "v$VERSION")"
    if [ "$tag_commit" != "$head_commit" ]; then
      echo "tag v$VERSION does not point to HEAD" >&2
      exit 1
    fi
    release_protocol="file"
    release_git clone -q --no-checkout --no-local "$ROOT" "$SOURCE_ROOT"
  fi

  sanitized_git -C "$ARCHIVE_REPO" archive "v$VERSION" | tar -x -C "$SOURCE_ROOT"
  sanitized_git -C "$SOURCE_ROOT" reset -q --mixed "$tag_commit"
  while IFS= read -r tag_ref; do
    sanitized_git -C "$SOURCE_ROOT" show-ref --verify --quiet "$tag_ref" || {
      echo "reconstructed release checkout is missing source tag ${tag_ref#refs/tags/}" >&2
      exit 1
    }
  done < <(sanitized_git -C "$ARCHIVE_REPO" for-each-ref --format='%(refname)' refs/tags/)
  if ! archive_status="$(isolated_status "$SOURCE_ROOT" "$tag_commit")"; then
    echo "release validation could not inspect the reconstructed release checkout" >&2
    exit 1
  fi
  if [ -n "$archive_status" ]; then
    echo "archived payload differs from tag v$VERSION" >&2
    exit 1
  fi
  if [ "$existing_mode" = "1" ] && [ -z "${GO_PROJECT_TEMPLATE:-}" ]; then
    template_commit=""
    case "$normalized_release_remote" in
      https://*)
        template_commit="$(python3 - "$SOURCE_ROOT" "$VERSION" <<'PYLOOKUP'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
from go_workflow.release_pairings import load_manifest, pairing_for
print(pairing_for(load_manifest(root / 'release-pairings.json'), 'v' + sys.argv[2])['template_commit'] or '')
PYLOOKUP
)"
        ;;
    esac
    if [ -n "$template_commit" ]; then
      template_root="$ARCHIVE_WORK/go-project-template"
      release_git clone -q --no-checkout --no-local \
        https://github.com/viggomeesters/go-project-template.git "$template_root"
      if ! sanitized_git -C "$template_root" cat-file -e "$template_commit^{commit}" 2>/dev/null; then
        echo "immutable project-template commit $template_commit is absent from the canonical origin" >&2
        exit 1
      fi
      if [ -z "$(sanitized_git -C "$template_root" for-each-ref --format='%(refname)' --contains "$template_commit" refs/remotes/origin/)" ]; then
        echo "immutable project-template commit $template_commit is not reachable from an origin branch" >&2
        exit 1
      fi
      sanitized_git -C "$template_root" checkout -q --detach "$template_commit"
      GO_PROJECT_TEMPLATE="$template_root"
    else
      GO_PROJECT_TEMPLATE="$ARCHIVE_WORK/no-project-template"
    fi
  elif [ -d "$ROOT/../go-project-template/.go" ]; then
    mkdir -p "$ARCHIVE_WORK/go-project-template"
    cp -a "$ROOT/../go-project-template/." "$ARCHIVE_WORK/go-project-template/"
    rm -rf "$ARCHIVE_WORK/go-project-template/.git"
    if [ -z "${GO_PROJECT_TEMPLATE:-}" ]; then
      GO_PROJECT_TEMPLATE="$ARCHIVE_WORK/go-project-template"
    fi
  fi
  echo "tag: annotated v$VERSION payload reconstructed from git archive with tag refs"
else
  if [ "$ALLOW_CANDIDATE" != "1" ]; then
    echo "annotated tag v$VERSION is required; use --allow-candidate only for the pre-tag candidate gate" >&2
    exit 1
  fi
  echo "tag: v$VERSION candidate mode; final gate still requires the annotated tag"
  # Run fixture-creating checks outside a registered task worker. The checkout
  # remains the source of truth; the disposable clone receives its exact
  # unstaged worktree delta (including untracked files, modes and symlinks).
  # An index delta cannot be reconstructed by copying files: refuse it.
  if ! sanitized_git -C "$ROOT" diff --cached --quiet --exit-code HEAD --; then
    echo "candidate index differs from HEAD; staged changes require a separate verified candidate" >&2
    exit 1
  fi
  ARCHIVE_WORK="$(mktemp -d)"
  SOURCE_ROOT="$ARCHIVE_WORK/go-workflow-stack"
  head_commit="$(sanitized_git -C "$ROOT" rev-parse HEAD)"
  release_remote="$(release_origin_url)" || exit 1
  release_protocol="file"
  release_git clone -q --no-local --no-checkout "$ROOT" "$SOURCE_ROOT"
  sanitized_git -C "$SOURCE_ROOT" checkout -q --detach "$head_commit"
  sanitized_git -C "$SOURCE_ROOT" remote set-url origin "$release_remote"
  python3 - "$ROOT" "$SOURCE_ROOT" "$ARCHIVE_WORK/caller-manifest.json" <<'PYCANDIDATE'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

source, target, manifest_path = map(Path, sys.argv[1:])
env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_OPTIONAL_LOCKS': '0'}
def git(*args):
    return subprocess.check_output(['/usr/bin/git', '--no-replace-objects', '-C', str(source), *args], env=env)

status = git('-c', 'status.showUntrackedFiles=all', 'status', '--porcelain=v1', '-z', '--untracked-files=all')
ignored = git('ls-files', '--others', '--ignored', '--exclude-standard', '-z')
parts = status.split(b'\0')
if parts[-1:] != [b'']:
    raise SystemExit('incomplete candidate status')
paths = []
i = 0
while i < len(parts) - 1:
    record = parts[i]
    code = record[:2]
    if code[:1] in (b'R', b'C') or code[1:] in (b'R', b'C'):
        raise SystemExit('candidate rename/copy requires an explicit release reconciliation')
    path = os.fsdecode(record[3:])
    if path.startswith('/') or '..' in Path(path).parts or path == '.git':
        raise SystemExit('unsafe candidate path')
    paths.append(path)
    i += 1

for raw in ignored.split(b'\0'):
    if raw:
        path = os.fsdecode(raw)
        if path.startswith('/') or '..' in Path(path).parts or path == '.git':
            raise SystemExit('unsafe ignored candidate path')
        paths.append(path)

def fingerprint(path):
    if path.is_symlink():
        return ['symlink', os.readlink(path)]
    if not path.exists():
        return ['deleted']
    if not path.is_file():
        raise SystemExit(f'unsupported candidate file type: {path}')
    return ['file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777]

before = {path: fingerprint(source / path) for path in paths}
index_path = Path(os.fsdecode(git('rev-parse', '--path-format=absolute', '--git-path', 'index').strip()))
index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
for path in paths:
    original, copied = source / path, target / path
    if any((target / parent).is_symlink() for parent in Path(path).parents if str(parent) != '.'):
        raise SystemExit('candidate parent symlink')
    if copied.is_symlink() or copied.is_file():
        copied.unlink()
    if original.is_symlink():
        copied.parent.mkdir(parents=True, exist_ok=True)
        copied.symlink_to(os.readlink(original))
    elif original.exists():
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, copied, follow_symlinks=False)
    if fingerprint(copied) != before[path]:
        raise SystemExit(f'candidate copy mismatch: {path}')
manifest_path.write_text(json.dumps({'head': git('rev-parse', 'HEAD').decode().strip(),
                                     'status': status.hex(), 'ignored': ignored.hex(), 'index': index_hash,
                                     'paths': before}, sort_keys=True))
PYCANDIDATE
  # A gate must never get a writable path to the caller's sibling template.
  caller_template="${GO_PROJECT_TEMPLATE:-$ROOT/../go-project-template}"
  GO_PROJECT_TEMPLATE="$ARCHIVE_WORK/go-project-template"
  if [ -d "$caller_template/.go" ]; then
    python3 - "$caller_template" "$GO_PROJECT_TEMPLATE" <<'PYCOPYTEMPLATE'
import os
from pathlib import Path
import shutil
import subprocess
import sys

source, target = map(Path, sys.argv[1:])
if source.is_symlink():
    raise SystemExit('candidate template symlink is not isolated')
for base, dirs, files in os.walk(source, followlinks=False):
    if any((Path(base) / name).is_symlink() for name in dirs + files):
        raise SystemExit('candidate template contains an external-capable symlink')
if subprocess.run(['git', '-C', str(source), 'diff', '--cached', '--quiet'], check=False).returncode:
    raise SystemExit('candidate template index differs from HEAD; reconcile staged input first')
head = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
subprocess.run(['git', 'clone', '--no-local', '--quiet', str(source), str(target)], check=True)
if subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip() != head:
    raise SystemExit('candidate template clone HEAD differs from source')
if not (target / '.git').is_dir():
    raise SystemExit('candidate template clone does not own its Git metadata')
# Clone owns its object store and refs, even when source/.git is a linked-worktree
# gitdir file. Replace only its worktree with the caller's exact current bytes.
for entry in target.iterdir():
    if entry.name == '.git':
        continue
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    else:
        entry.unlink()
shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns('.git'))
# Keep commits/tags and fetch provenance, but never inherit a writable push
# destination from the caller's template checkout.
source_origin = subprocess.run(['git', '-C', str(source), 'remote', 'get-url', 'origin'],
                               text=True, capture_output=True, check=False)
if source_origin.returncode == 0:
    subprocess.run(['git', '-C', str(target), 'remote', 'set-url', 'origin',
                    source_origin.stdout.strip()], check=True)
remotes = subprocess.check_output(['git', '-C', str(target), 'remote'], text=True).splitlines()
for remote in remotes:
    subprocess.run(['git', '-C', str(target), 'config', '--local', '--unset-all',
                    f'remote.{remote}.pushurl'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['git', '-C', str(target), 'config', '--local', '--add',
                    f'remote.{remote}.pushurl', str(target / '.no-upstream')], check=True)
PYCOPYTEMPLATE
  fi
fi

(
  cd "$SOURCE_ROOT"
  sanitized_gate python3 - "$SOURCE_ROOT" "$VERSION" <<'PYPAIRING'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.dont_write_bytecode = True
sys.path.insert(0, str(root))
from go_workflow.release_pairings import load_manifest
load_manifest(root / 'release-pairings.json', expected_ref='v' + sys.argv[2])
PYPAIRING
  sanitized_gate /usr/bin/env \
    GO_PROJECT_TEMPLATE="${GO_PROJECT_TEMPLATE:-$ROOT/../go-project-template}" \
    "$BASH_BIN" "$SOURCE_ROOT/scripts/check-linux.sh"
  sanitized_gate "$BASH_BIN" "$SOURCE_ROOT/scripts/check-distribution.sh" "$SOURCE_ROOT"
)

if [ "$ALLOW_CANDIDATE" = "1" ] && [ "$EXISTING_MODE" != "1" ] && [ "$local_tag_present" = "0" ]; then
  python3 - "$ROOT" "$ARCHIVE_WORK/caller-manifest.json" <<'PYREADBACK'
import hashlib, json, os, pathlib, subprocess, sys
root, manifest_path = map(pathlib.Path, sys.argv[1:])
before = json.loads(manifest_path.read_text())
def git(*args):
    return subprocess.check_output(['/usr/bin/git', '--no-replace-objects', '-C', str(root), *args])
assert git('rev-parse', 'HEAD').decode().strip() == before['head'], 'candidate gate changed caller HEAD'
assert git('-c', 'status.showUntrackedFiles=all', 'status', '--porcelain=v1', '-z', '--untracked-files=all').hex() == before['status'], 'candidate gate changed caller status'
assert git('ls-files', '--others', '--ignored', '--exclude-standard', '-z').hex() == before['ignored'], 'candidate gate changed caller ignored files'
index = pathlib.Path(os.fsdecode(git('rev-parse', '--path-format=absolute', '--git-path', 'index').strip()))
assert hashlib.sha256(index.read_bytes()).hexdigest() == before['index'], 'candidate gate changed caller index'
for name, expected in before['paths'].items():
    path = root / name
    if path.is_symlink(): actual = ['symlink', os.readlink(path)]
    elif not path.exists(): actual = ['deleted']
    else: actual = ['file', hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777]
    assert actual == expected, f'candidate gate changed caller file: {name}'
PYREADBACK
elif [ -n "${head_commit:-}" ]; then
  current_head="$(sanitized_git -C "$ROOT" rev-parse HEAD)"
  if [ "$current_head" != "$head_commit" ]; then
    echo "release validation changed the caller HEAD" >&2
    exit 1
  fi
  if ! post_gate_status="$(isolated_status "$ROOT" "$head_commit")"; then
    echo "release validation could not re-inspect the caller worktree after gates" >&2
    exit 1
  fi
  if [ -n "$post_gate_status" ]; then
    echo "release validation gates changed the caller checkout" >&2
    exit 1
  fi
fi

echo "publish: not performed"
