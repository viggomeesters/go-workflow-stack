#!/usr/bin/python3 -I
"""Security boundary for the Bash release validator."""

import os
import pwd
import subprocess
import sys
import tempfile


script_path = os.path.realpath(sys.argv[0])
script_dir = os.path.dirname(script_path)
arguments = sys.argv[1:]
if len(arguments) >= 2 and arguments[0] == "--repo":
    root = os.path.realpath(arguments[1])
    arguments = arguments[2:]
else:
    root = os.path.dirname(script_dir)
inner = os.path.join(root, "scripts", "release-check-inner.sh")
if not os.path.isfile(inner):
    raise SystemExit(f"release validator is missing {inner}")

bash_bin = next((path for path in ("/usr/bin/bash", "/bin/bash") if os.access(path, os.X_OK)), None)
if bash_bin is None:
    raise SystemExit("release validation requires Bash at /usr/bin/bash or /bin/bash")

try:
    trusted_home = pwd.getpwuid(os.getuid()).pw_dir
except KeyError:
    trusted_home = "/tmp"
if not os.path.isdir(trusted_home):
    trusted_home = "/tmp"

project_template_set = "1" if "GO_PROJECT_TEMPLATE" in os.environ else "0"
project_template = os.environ.get("GO_PROJECT_TEMPLATE", "")
clean_env = {
    "HOME": trusted_home,
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "TMPDIR": "/tmp",
    "PYTHONNOUSERSITE": "1",
    "PYTHONSAFEPATH": "1",
    "GO_PROJECT_TEMPLATE_SET": project_template_set,
    "GO_PROJECT_TEMPLATE": project_template,
    "GO_RELEASE_ROOT": root,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
}


def trusted_git(*git_arguments, cwd=None, env=None):
    return subprocess.run(
        ["/usr/bin/git", "--no-replace-objects", "--no-pager", *git_arguments],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env or clean_env,
    )


def command_error(exc, fallback):
    return exc.stderr.decode("utf-8", errors="replace").strip() or fallback


external_launcher = os.path.commonpath((script_path, root)) != root
existing_mode = "--validate-existing" in arguments
allow_local_origin = "--allow-local-origin" in arguments
expected_inner = None
try:
    head_commit = trusted_git("-C", root, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
    if external_launcher:
        try:
            origin_url = trusted_git("-C", root, "config", "--local", "--get", "remote.origin.url").stdout.decode().strip()
        except subprocess.CalledProcessError:
            raise SystemExit("release launcher requires an origin remote before executing repository code")
        normalized_origin = origin_url.strip().lower().rstrip("/")
        if normalized_origin.endswith(".git"):
            normalized_origin = normalized_origin[:-4]
        official_origin = normalized_origin == "https://github.com/viggomeesters/go-workflow-stack"
        local_origin = origin_url.startswith("/") or origin_url.lower().startswith("file://")
        if not official_origin and not (allow_local_origin and local_origin):
            raise SystemExit(f"release launcher refuses non-official origin {origin_url}")
        if existing_mode:
            protocol = "file" if local_origin else "https"
            clone_env = {**clean_env, "GIT_ALLOW_PROTOCOL": protocol}
            with tempfile.TemporaryDirectory(prefix="go-release-launcher-") as remote_repo:
                trusted_git("clone", "-q", "--bare", "--no-local", origin_url, remote_repo, env=clone_env)
                trusted_git("--git-dir", remote_repo, "cat-file", "-e", f"{head_commit}^{{commit}}")
                containing_refs = trusted_git(
                    "--git-dir", remote_repo, "for-each-ref", "--format=%(refname)",
                    "--contains", head_commit, "refs/heads/",
                ).stdout
                if not containing_refs.strip():
                    raise SystemExit(f"release launcher refuses HEAD {head_commit} absent from origin branches")
                expected_inner = trusted_git(
                    "--git-dir", remote_repo, "show", f"{head_commit}:scripts/release-check-inner.sh",
                ).stdout
    if expected_inner is None:
        expected_inner = trusted_git("-C", root, "show", f"{head_commit}:scripts/release-check-inner.sh").stdout
except subprocess.CalledProcessError as exc:
    raise SystemExit(command_error(exc, "cannot establish release launcher provenance"))
with open(inner, "rb") as handle:
    actual_inner = handle.read()
if actual_inner != expected_inner:
    raise SystemExit("release validator differs from HEAD; refusing mutable inner payload")
if not hasattr(os, "memfd_create"):
    raise SystemExit("release validation requires Linux memfd support")
inner_fd = os.memfd_create("go-release-check-inner")
os.write(inner_fd, expected_inner)
os.lseek(inner_fd, 0, os.SEEK_SET)
os.set_inheritable(inner_fd, True)
verified_inner = f"/proc/self/fd/{inner_fd}"
os.execve(bash_bin, [bash_bin, verified_inner, *arguments], clean_env)
