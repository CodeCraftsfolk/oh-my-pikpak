#!/usr/bin/env python3
"""Install search-videos-save-to-pikpak with optional PikPak skill and local invitation setup."""

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


INSTALLER_URL = "https://download.mypikpak.com/cli/install.sh"
PLACEHOLDER = "YOUR_AFFILIATE_CODE"
SAFE_ENV = {
    "PIKPAK_NO_UPDATE_NOTIFIER": "1",
    "PIKPAK_NO_LOG": "1",
    "PIKPAK_NO_SKILL_SYNC": "1",
}


class SetupError(Exception):
    pass


def parse_args():
    parser = argparse.ArgumentParser(
        description="Explicitly install search-videos-save-to-pikpak without overwriting a different skill.",
        epilog="Copying SKILL.md alone does not execute setup. PikPak is optional; authentication is opt-in.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print JSON actions; do not write files or run commands")
    parser.add_argument("--skip-pikpak", action="store_true", help="Install only search-videos-save-to-pikpak; skip PikPak CLI, skill and invitation setup")
    referral = parser.add_mutually_exclusive_group()
    referral.add_argument("--affiliate", metavar="CODE", help="Override the invitation code for this setup without changing the bundled config")
    referral.add_argument("--no-affiliate", dest="affiliate", action="store_const", const="", help="Do not record an invitation code; register without one if --auth register is used")
    parser.add_argument("--auth", choices=("none", "login", "register"), default="none", help="Explicit authentication action (default: none)")
    parser.add_argument("--skills-dir", type=Path, default=Path.home() / ".agents" / "skills", help="Parent of search-videos-save-to-pikpak (default: ~/.agents/skills)")
    return parser.parse_args()


def require_writable_parent(path):
    parent = path
    while not parent.exists():
        if parent.is_symlink():
            raise SetupError("Dangling destination ancestor: {}".format(parent))
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise SetupError("Destination parent is not a writable directory: {}".format(parent))


def read_payload(source, affiliate):
    scripts = source / "scripts"
    if scripts.is_symlink() or not scripts.is_dir():
        raise SetupError("The source scripts directory must be a real directory")
    paths = [source / "SKILL.md", source / "config.json"] + sorted(scripts.glob("*.py"))
    payload = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise SetupError("Required source must be a regular file: {}".format(path))
        payload[path.relative_to(source).as_posix()] = path.read_bytes()
    config = json.loads(payload["config.json"])
    if not isinstance(config, dict) or not isinstance(config.get("affiliate_code"), str):
        raise SetupError("config.json must contain a string affiliate_code")
    code = (config["affiliate_code"] if affiliate is None else affiliate).strip()
    if "\0" in code:
        raise SetupError("Referral code cannot contain a NUL character")
    if code.upper() == PLACEHOLDER:
        raise SetupError("Replace YOUR_AFFILIATE_CODE in config.json or use --no-affiliate")
    return payload, code


def destination_state(destination, payload):
    if destination.is_symlink():
        raise SetupError("Refusing a symlink skill destination: {}".format(destination))
    if not destination.exists():
        require_writable_parent(destination.parent)
        return "install"
    if not destination.is_dir():
        raise SetupError("Skill destination already exists and is not a directory")
    expected = set(payload) | {"scripts"}
    observed = set()
    for root, dirs, files in os.walk(destination, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            relative = path.relative_to(destination).as_posix()
            if path.is_symlink() or relative not in expected:
                raise SetupError("Refusing a different or foreign skill destination: {}".format(destination))
            observed.add(relative)
            if relative in payload and (not path.is_file() or path.read_bytes() != payload[relative]):
                raise SetupError("Refusing to overwrite modified skill content: {}".format(path))
    if observed != expected:
        raise SetupError("Refusing an incomplete or foreign skill destination: {}".format(destination))
    return "identical"


def discover_pikpak():
    found = shutil.which("pikpak")
    if found:
        return str(Path(found).absolute())
    fallback = Path.home() / ".local" / "bin" / "pikpak"
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    return None


def invitation_path():
    if os.environ.get("PIKPAK_DIR"):
        return Path(os.environ["PIKPAK_DIR"]).absolute() / "affiliate"
    if os.environ.get("PIKPAK_CREDENTIALS"):
        return Path(os.environ["PIKPAK_CREDENTIALS"]).absolute().parent / "affiliate"
    return Path.home() / ".pikpak" / "affiliate"


def record_invitation(path, code, overwrite):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".affiliate-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(code)
            handle.close()
            if overwrite:
                os.replace(temporary, path)
            else:
                # Publish atomically without replacing a code created since preflight.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    if path.is_file() and not path.is_symlink() and not path.read_text(encoding="utf-8").strip():
                        os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def preflight(args):
    if args.skip_pikpak and args.auth != "none":
        raise SetupError("--skip-pikpak cannot be combined with --auth login/register")
    source = Path(__file__).resolve().parent.parent
    payload, code = read_payload(source, args.affiliate)
    destination = args.skills_dir.expanduser().absolute() / "search-videos-save-to-pikpak"
    state = destination_state(destination, payload)
    plan = {
        "dry_run": args.dry_run,
        "destination": str(destination),
        "skill_action": state,
        "auth": args.auth,
        "actions": [],
    }
    executable = None
    install_dir = Path.home() / ".local" / "bin"
    if not args.skip_pikpak:
        path = invitation_path()
        skip_referral = args.affiliate is not None and not code
        saved = path.read_text(encoding="utf-8").strip() if path.is_file() and not skip_referral else ""
        if skip_referral:
            referral_action = "disabled"
        elif saved and (args.affiliate is None or saved == code):
            code = saved
            referral_action = "preserve"
        else:
            referral_action = "record" if code else "unconfigured"
        plan["referral"] = {"action": referral_action, "path": str(path), "code": code}
        if code:
            plan["referral_disclosure"] = (
                "This setup uses invitation code {} for the next 'pikpak auth register'; "
                "only registration sends it to PikPak. Existing codes are preserved unless "
                "--affiliate explicitly overrides them. No account is created during installation; "
                "no reward or benefit is promised. Use --no-affiliate to opt out of this setup's referral."
            ).format(code)
        executable = discover_pikpak()
        if executable is None:
            if platform.system() not in ("Darwin", "Linux"):
                raise SetupError("Automatic PikPak installation supports macOS/Linux only; install it manually or use --skip-pikpak")
            if platform.machine().lower() not in ("x86_64", "amd64", "arm64", "aarch64"):
                raise SetupError("The official installer supports amd64/arm64 only")
            for command in ("curl", "bash", "uname", "tr", "sed", "head", "mktemp", "chmod", "mv", "mkdir", "dirname", "basename", "grep", "tail", "rm"):
                if shutil.which(command) is None:
                    raise SetupError("Official installer prerequisite missing: {}".format(command))
            target = install_dir / "pikpak"
            if target.exists() or target.is_symlink():
                raise SetupError("Refusing to replace a non-executable PikPak path: {}".format(target))
            require_writable_parent(install_dir)
            executable = str(target)
            plan["actions"].extend([
                {"action": "download_installer", "argv": [shutil.which("curl"), "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https", "--output", "<temporary-installer.sh>", INSTALLER_URL]},
                {"action": "install_pikpak", "argv": [shutil.which("bash"), "<temporary-installer.sh>", "--affiliate", code if referral_action == "record" else ""], "environment": {"PIKPAK_INSTALL_DIR": str(install_dir), "PATH": "{}:<inherited PATH>".format(install_dir), **SAFE_ENV}},
            ])
        elif referral_action == "record":
            plan["actions"].append({"action": "record_invitation", "path": str(path), "overwrite": args.affiliate is not None})
        plan["actions"].extend([
            {"action": "verify_pikpak", "argv": [executable, "version"], "environment": SAFE_ENV.copy()},
            {"action": "install_pikpak_skill", "argv": [executable, "skill", "install", "--dir", str(destination.parent)], "environment": SAFE_ENV.copy()},
        ])
    plan["actions"].append({"action": "install_skill" if state == "install" else "keep_identical_skill", "files": sorted(payload), "destination": str(destination)})
    if args.auth != "none":
        argv = [executable, "auth", args.auth]
        if args.auth == "register" and plan["referral"]["action"] == "disabled":
            argv.append("--affiliate=")
        plan["actions"].append({"action": "authenticate", "argv": argv, "environment": SAFE_ENV.copy()})
    return plan, payload, code, destination, executable, install_dir


def run_command(argv, env, label, capture=False):
    result = subprocess.run(
        argv, env=env, check=False,
        stdout=subprocess.PIPE if capture else sys.stderr,
        stderr=subprocess.PIPE if capture else sys.stderr,
        text=True,
    )
    if result.returncode != 0:
        raise SetupError("{} failed with exit code {}".format(label, result.returncode))
    if capture and not result.stdout.strip():
        raise SetupError("{} returned no version information".format(label))


def install_skill(destination, payload):
    # Recheck after dependency installation. Never merge into an existing directory.
    if destination_state(destination, payload) == "identical":
        return "identical"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(exist_ok=False)
    (destination / "scripts").mkdir()
    for relative, content in payload.items():
        with (destination / relative).open("xb") as handle:
            handle.write(content)
    return "installed"


def execute(plan, payload, code, destination, executable, install_dir):
    env = os.environ.copy()
    env.update(SAFE_ENV)
    if "referral_disclosure" in plan:
        print(plan["referral_disclosure"], file=sys.stderr)
    if any(action["action"] == "download_installer" for action in plan["actions"]):
        env["PIKPAK_INSTALL_DIR"] = str(install_dir)
        # The official installer skips shell-profile writes when this path is present.
        env["PATH"] = str(install_dir) + os.pathsep + env.get("PATH", "")
        with tempfile.TemporaryDirectory(prefix="search-videos-save-to-pikpak-setup-") as temporary:
            installer = str(Path(temporary) / "install.sh")
            for action in plan["actions"][:2]:
                argv = [installer if value == "<temporary-installer.sh>" else value for value in action["argv"]]
                run_command(argv, env, action["action"])
        if not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise SetupError("Official installer did not produce an executable PikPak binary")
    if executable:
        run_command([executable, "version"], env, "PikPak version verification", capture=True)
        for action in plan["actions"]:
            if action["action"] == "record_invitation":
                try:
                    record_invitation(Path(action["path"]), code, action["overwrite"])
                except OSError as error:
                    print("Warning: could not record invitation code: {}".format(error), file=sys.stderr)
        referral = plan["referral"]
        if referral["action"] in ("record", "preserve"):
            try:
                saved = Path(referral["path"]).read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                saved = ""
            # A concurrent setup may have recorded another code; respect it by default.
            if saved and any(action["action"] == "record_invitation" and not action["overwrite"] for action in plan["actions"]):
                code = saved
                referral["code"] = saved
            referral["recorded"] = saved == code
            if not referral["recorded"]:
                warning = "Invitation code was not saved. Register with 'pikpak auth register --affiliate <code>' using the code in referral.code."
                plan.setdefault("warnings", []).append(warning)
                print("Warning: " + warning, file=sys.stderr)
        run_command([executable, "skill", "install", "--dir", str(destination.parent)], env, "PikPak skill installation")
    plan["skill_action"] = install_skill(destination, payload)
    if plan["auth"] != "none":
        action = next(action for action in plan["actions"] if action["action"] == "authenticate")
        if plan["auth"] == "register" and plan["referral"].get("recorded") is False:
            action["argv"] += ["--affiliate", code]
        run_command(action["argv"], env, "PikPak authentication")
    plan["status"] = "complete"


def main():
    args = parse_args()
    try:
        plan, payload, code, destination, executable, install_dir = preflight(args)
        if args.dry_run:
            plan["status"] = "planned"
        else:
            execute(plan, payload, code, destination, executable, install_dir)
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    except (SetupError, OSError, ValueError) as error:
        print(json.dumps({"error": {"code": "SETUP_FAILED", "message": str(error)}}, ensure_ascii=False))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"error": {"code": "INTERRUPTED", "message": "Setup interrupted; already completed actions are not rolled back"}}))
        return 130


if __name__ == "__main__":
    sys.exit(main())
