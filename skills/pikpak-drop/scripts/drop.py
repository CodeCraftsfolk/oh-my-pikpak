#!/usr/bin/env python3
"""pikpak-drop: upload local files/folders to PikPak and share them under one link.

stdout: exactly one JSON object (success or error). stderr: progress for humans.

Exit codes:
  0  done
  1  PikPak/runtime failure (error.stage tells where; items already uploaded are listed)
  2  usage error (bad arguments, missing local or --cloud path, nothing to upload)
  3  blocked before any upload; needs a user decision (FLAGGED_FILES, INSUFFICIENT_SPACE)
  4  environment (NOT_INSTALLED, NOT_LOGGED_IN or other auth failures)
"""
from __future__ import annotations

import argparse
import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_FOLDER = "Drops"
DEFAULT_EXPIRE_DAYS = 7

# Referral code shipped with the skill (<skill-dir>/config.json). It is only ever used
# when an account has to be created from scratch; PIKPAK_AFFILIATE overrides it, and a
# blank or placeholder value means "no referral" rather than sending junk to PikPak.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
AFFILIATE_PLACEHOLDER = "YOUR_AFFILIATE_CODE"
INSTALL_SH_URL = "https://download.mypikpak.com/cli/install.sh"
INSTALL_PS1_URL = "https://download.mypikpak.com/cli/install.ps1"

# Matched case-insensitively against file names.
SECRET_NAME_PATTERNS = (
    (".env", "environment file, often holds API keys"),
    (".env.*", "environment file, often holds API keys"),
    ("id_rsa*", "SSH private key"),
    ("id_dsa*", "SSH private key"),
    ("id_ecdsa*", "SSH private key"),
    ("id_ed25519*", "SSH private key"),
    ("*.p12", "certificate bundle with private key"),
    ("*.pfx", "certificate bundle with private key"),
    ("*.jks", "Java keystore"),
    ("*.keystore", "keystore"),
    (".npmrc", "npm registry token"),
    (".pypirc", "PyPI upload token"),
    (".netrc", "saved login credentials"),
    (".git-credentials", "saved git credentials"),
    ("credentials", "credentials file"),
    ("credentials.json", "credentials file"),
    ("*service-account*.json", "cloud service-account key"),
    ("*.kdbx", "password database"),
    (".htpasswd", "password hashes"),
    ("*.tfstate", "Terraform state, may contain secrets"),
    ("*.tfstate.backup", "Terraform state, may contain secrets"),
    ("*.tfvars", "Terraform variables, often hold secrets"),
    ("secrets.*", "secrets file"),
    ("*.ppk", "PuTTY private key"),
    ("*.ovpn", "VPN profile, may embed keys"),
    ("kubeconfig", "Kubernetes cluster credentials"),
    (".pgpass", "PostgreSQL passwords"),
    (".my.cnf", "MySQL credentials"),
    ("*.keychain", "macOS keychain"),
    ("*.keychain-db", "macOS keychain"),
    ("wallet.dat", "cryptocurrency wallet"),
)
# These are only flagged when they actually hold a private key: Keynote decks
# are .key files too, and certificates or public PGP keys are fine to share.
PEM_SNIFF_SUFFIXES = (".key", ".pem", ".p8", ".asc")
SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".pub")
FLAGGED_DIRS = {
    ".git": "git repository data (full history, may include secrets)",
    ".ssh": "SSH keys directory",
    ".aws": "AWS credentials directory",
    ".gnupg": "GPG keyring",
    ".kube": "Kubernetes credentials directory",
    ".docker": "Docker registry credentials directory",
    "node_modules": "dependency folder (large, rarely meant to be shared)",
}
# OS clutter nobody means to send; skipped silently inside folders.
JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
AUTH_ERROR_CODES = {
    "UNAUTHENTICATED",
    "UNAUTHORIZED",
    "TOKEN_EXPIRED",
    "TOKEN_REFRESH_FAILED",
    "CREDENTIALS_ERROR",
    "ACCESS_DENIED",
}
# Network hiccups worth retrying. Uploads are never retried automatically:
# a lost response after a finished upload would create a name(1) duplicate.
TRANSIENT_ERROR_CODES = {"REQUEST_FAILED", "TIMEOUT", "NETWORK_ERROR"}
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "PB": 1024**5}


class DropError(Exception):
    def __init__(self, exit_code, code, message, hint="", stage="", details=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.code = code
        self.message = message
        self.hint = hint
        self.stage = stage
        self.details = details or {}

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.stage:
            out["stage"] = self.stage
        if self.details:
            out["details"] = self.details
        return out


class JsonArgumentParser(argparse.ArgumentParser):
    """argparse that reports usage errors as JSON on stdout, like every other failure."""

    def error(self, message):
        self.print_usage(sys.stderr)
        emit({"ok": False, "error": {"code": "USAGE_ERROR", "message": message,
                                     "hint": "Run with --help for the full option list."}})
        sys.exit(2)


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()


def log(message):
    print(message, file=sys.stderr, flush=True)


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def parse_size(text):
    match = re.fullmatch(r"\s*([\d.]+)\s*([KMGTP]?B)\s*", str(text or ""), re.IGNORECASE)
    if not match:
        return None
    return int(float(match.group(1)) * SIZE_UNITS[match.group(2).upper()])


def _json_or_none(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def affiliate_code():
    """The referral code to use for a brand-new account, or "" when none is configured."""
    code = os.environ.get("PIKPAK_AFFILIATE")
    if code is None:
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            code = config.get("affiliate_code") if isinstance(config, dict) else None
        except (OSError, ValueError):
            code = None
    code = str(code or "").strip()
    if not code or code.upper() == AFFILIATE_PLACEHOLDER or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", code):
        return ""
    return code


def setup_details(installed):
    """Return setup commands that never apply a referral without consent."""
    code = affiliate_code()
    details = {
        "install": {
            "macos_linux": f"curl -fsSL {INSTALL_SH_URL} | bash",
            "windows": f"irm {INSTALL_PS1_URL} | iex",
            "homebrew": "brew install pikcloud/tap/pikpak-cli",
        },
        "existing_account": "pikpak auth login",
        "new_account": "pikpak auth register --affiliate=",
    }
    if installed:
        details.pop("install")
    if code:
        details["affiliate_code"] = code
        details["disclosure"] = (f"The optional referral code {code} is sent to PikPak during "
                                 "registration. Ask for explicit consent before using it; "
                                 "no reward is guaranteed.")
        details["new_account_with_affiliate"] = f"pikpak auth register --affiliate {shlex.quote(code)}"
    return details


def run_pikpak(args, stage, retry=True):
    attempts = 3 if retry else 1
    for attempt in range(1, attempts + 1):
        try:
            return _run_pikpak_once(args, stage)
        except DropError as exc:
            if exc.code not in TRANSIENT_ERROR_CODES or attempt == attempts:
                raise
            log(f"PikPak: {exc.message} ({exc.code}); retrying ...")
            time.sleep(3 * attempt)


def _run_pikpak_once(args, stage):
    env = {**os.environ, "PIKPAK_NO_UPDATE_NOTIFIER": "1"}
    try:
        proc = subprocess.run(["pikpak", *args], capture_output=True, text=True, env=env)
    except FileNotFoundError:
        raise DropError(4, "NOT_INSTALLED", "The pikpak CLI is not on PATH.",
                        hint="Install it (see the skill's Setup section), then re-run.", stage=stage,
                        details={"setup": setup_details(installed=False)})
    if proc.returncode == 0:
        parsed = _json_or_none(proc.stdout)
        return parsed if parsed is not None else {"raw": proc.stdout.strip()}
    payload = _json_or_none(proc.stderr) or _json_or_none(proc.stdout) or {}
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = str(err.get("code") or "PIKPAK_ERROR").upper()
    message = err.get("reason") or err.get("message") or (proc.stderr or proc.stdout).strip()[:500]
    details = {"command": shlex.join(["pikpak", *args])}
    if code in AUTH_ERROR_CODES:
        details["setup"] = setup_details(installed=True)
    raise DropError(4 if code in AUTH_ERROR_CODES else 1, code, message,
                    hint=err.get("hint", ""), stage=stage, details=details)


def secret_reason(path: Path):
    name = path.name.lower()
    if name.endswith(SAFE_SUFFIXES):
        return None
    if name.endswith(PEM_SNIFF_SUFFIXES):
        try:
            with open(path, "rb") as fh:
                head = fh.read(8192)
        except OSError:
            return None
        return "private key" if b"PRIVATE KEY" in head else None
    for pattern, reason in SECRET_NAME_PATTERNS:
        if fnmatch.fnmatchcase(name, pattern):
            return reason
    return None


def matches_exclude(patterns, *candidates):
    for pattern in patterns:
        for candidate in candidates:
            if fnmatch.fnmatchcase(candidate, pattern):
                return True
    return False


def is_junk(name):
    return name.lower() in JUNK_NAMES or name.startswith("._")


def scan_local(src: Path, excludes, allow_flagged):
    """Count what would be uploaded and collect flagged/excluded entries."""
    top = src.name
    item = {"source": str(src), "name": top, "kind": "folder" if src.is_dir() else "file",
            "files": 0, "bytes": 0, "flagged": [], "excluded": [], "skipped": 0, "kept": []}
    if not src.is_dir():
        reason = secret_reason(src)
        if reason:
            item["flagged"].append({"path": top, "reason": reason})
        item["files"], item["bytes"] = 1, src.stat().st_size
        return item

    for root, dirs, files in os.walk(src):
        root_path = Path(root)
        rel_root = root_path.relative_to(src)
        kept_dirs = []
        for name in sorted(dirs):
            rel = (rel_root / name).as_posix()
            shown = f"{top}/{rel}"
            if matches_exclude(excludes, shown, rel, name, str(root_path / name)):
                item["excluded"].append(shown + "/")
                continue
            reason = FLAGGED_DIRS.get(name.lower())
            if reason:
                item["flagged"].append({"path": shown + "/", "reason": reason})
                if not allow_flagged:
                    continue  # the run is blocked anyway; skip walking e.g. node_modules
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in sorted(files):
            if is_junk(name):
                item["skipped"] += 1
                continue
            file_path = root_path / name
            rel = (rel_root / name).as_posix()
            shown = f"{top}/{rel}"
            if matches_exclude(excludes, shown, rel, name, str(file_path)):
                item["excluded"].append(shown)
                continue
            reason = secret_reason(file_path)
            if reason:
                item["flagged"].append({"path": shown, "reason": reason})
            try:
                size = file_path.stat().st_size
            except OSError:
                continue  # broken symlink
            item["files"] += 1
            item["bytes"] += size
            item["kept"].append(rel)
    return item


def stage_folder(item, tmp_root: Path):
    """Recreate the folder under tmp_root without excluded entries (hardlinks, copy fallback)."""
    src = Path(item["source"])
    dst_top = tmp_root / src.name
    dst_top.mkdir(parents=True, exist_ok=True)
    for rel in item["kept"]:
        dst = dst_top / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src / rel, dst)
        except OSError:
            shutil.copy2(src / rel, dst)
    return dst_top


def list_children(parent_id, kind=None, stage="folder"):
    token, seen = None, set()
    while True:
        args = ["ls"] + ([parent_id] if parent_id else []) + ["-l", "500", "-F", "json"]
        if kind:
            args += ["--type", kind]
        if token:
            args += ["-t", token]
        page = run_pikpak(args, stage=stage)
        yield from page.get("files", [])
        token = page.get("next_page_token")
        if not token or token in seen:
            return
        seen.add(token)


def find_child_folder(parent_id, name, stage="folder"):
    for entry in list_children(parent_id, kind="folder", stage=stage):
        if entry.get("name") == name:
            return entry["id"]
    return None


def resolve_folder(path, create, stage="folder"):
    """Walk (and optionally create) a slash-separated folder path from the root.

    Returns (folder_id, created): folder_id is None when the path is missing and
    create is False; created is True when this call made any part of the path.
    """
    parent, created = "", False  # PikPak's root folder ID is the empty string
    for part in [p for p in path.strip("/").split("/") if p]:
        folder_id = find_child_folder(parent, part, stage)
        if folder_id is None:
            if not create:
                return None, False
            args = ["mkdir", part, "-F", "json"] + (["-p", parent] if parent else [])
            try:
                folder_id = run_pikpak(args, stage=stage)["id"]
                created = True
            except DropError as exc:
                if exc.code != "FILE_DUPLICATED_NAME":
                    raise
                folder_id = find_child_folder(parent, part, stage)  # created concurrently
                if folder_id is None:
                    raise
        parent = folder_id
    return parent, created


def resolve_cloud_path(path):
    """Find a file or folder already in PikPak by its path from the root."""
    parts = [p for p in path.strip().strip("/").split("/") if p]
    if not parts:
        raise DropError(2, "USAGE_ERROR", "--cloud needs a path such as 'Movies/trip.mp4'.", stage="preflight")
    parent_path, name = "/".join(parts[:-1]), parts[-1]
    parent_id, _ = resolve_folder(parent_path, create=False, stage="preflight")
    if parent_id is None:
        raise DropError(2, "CLOUD_PATH_NOT_FOUND", f"No folder '{parent_path}' in PikPak.",
                        hint="Paths start at the PikPak root; check each folder name with `pikpak ls`.",
                        stage="preflight")
    children = list(list_children(parent_id, stage="preflight"))
    matches = ([c for c in children if c.get("name") == name]
               or [c for c in children if c.get("name", "").lower() == name.lower()])
    if len(matches) != 1:
        similar = difflib.get_close_matches(name, [c.get("name", "") for c in children], n=5, cutoff=0.4)
        raise DropError(2, "CLOUD_PATH_AMBIGUOUS" if matches else "CLOUD_PATH_NOT_FOUND",
                        f"{'Several items' if matches else 'Nothing'} named '{name}' in PikPak/{parent_path}.",
                        hint="Use the exact name, or pass --id with the file ID from `pikpak ls`.",
                        stage="preflight", details={"folder": parent_path or "/", "similar": similar})
    entry = matches[0]
    return {"source": "pikpak:/" + "/".join(parts[:-1] + [entry["name"]]), "name": entry["name"],
            "kind": entry.get("kind", "file"), "id": entry["id"]}


def check_account(need_bytes):
    status = run_pikpak(["auth", "status", "-F", "json"], stage="preflight")
    if not status.get("logged_in"):
        raise DropError(4, "NOT_LOGGED_IN", "The pikpak CLI is not logged in.",
                        hint="An existing account logs in with `pikpak auth login` (device flow or a "
                             "PAT when there is no browser). Only a user without a PikPak account "
                             "registers — see the skill's Setup section.", stage="preflight",
                        details={"setup": setup_details(installed=True)})
    storage = status.get("storage") or {}
    if storage.get("is_unlimited"):
        return
    total, used = parse_size(storage.get("total")), parse_size(storage.get("used"))
    if total and used is not None and need_bytes > total - used:
        free = max(total - used, 0)
        raise DropError(3, "INSUFFICIENT_SPACE",
                        f"Needs {human_size(need_bytes)} but only {human_size(free)} is free on PikPak.",
                        hint="Free up space or upgrade the plan, then re-run.", stage="preflight",
                        details={"needed_bytes": need_bytes, "free_bytes": free,
                                 "storage": {"total": storage.get("total"), "used": storage.get("used")}})


def default_title(names, lang):
    if len(names) == 1:
        return names[0]
    if lang == "zh":
        return f"{names[0]} 等 {len(names)} 项"
    return f"{names[0]} + {len(names) - 1} more"


def build_message(title, url, password, expire_days, lang):
    if lang == "zh":
        lines = [title, f"链接：{url}"]
        if password:
            lines.append(f"提取码：{password}")
        lines.append("永久有效" if expire_days < 0 else f"有效期：{expire_days} 天")
    else:
        lines = [title, f"Link: {url}"]
        if password:
            lines.append(f"Password: {password}")
        if expire_days < 0:
            lines.append("Never expires")
        else:
            lines.append(f"Expires in {expire_days} day{'' if expire_days == 1 else 's'}")
    return "\n".join(lines)


def parse_args(argv):
    parser = JsonArgumentParser(
        description="Upload local files/folders to PikPak and share them under one link.")
    parser.add_argument("paths", nargs="*", help="Local files or folders to send")
    parser.add_argument("--cloud", dest="clouds", action="append", default=[], metavar="PATH",
                        help="Also share something already in PikPak, by path from the root, "
                             "e.g. 'Meetings/kickoff.mp4' (repeatable)")
    parser.add_argument("--id", dest="ids", action="append", default=[], metavar="FILE_ID",
                        help="Also share a file/folder already in PikPak, by ID (repeatable)")
    parser.add_argument("--folder",
                        help="PikPak folder path for the uploads, created if missing (default: a new "
                             f"dated folder inside {DEFAULT_FOLDER}, so repeated file names never clash)")
    parser.add_argument("--expire-days", type=int, default=DEFAULT_EXPIRE_DAYS,
                        help=f"Link lifetime in days, -1 = never (default: {DEFAULT_EXPIRE_DAYS})")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--public", action="store_true", help="No password on the link")
    access.add_argument("--password", help="Use this password instead of a random one")
    parser.add_argument("--title", help="Share title (default: item name)")
    parser.add_argument("--max-restores", type=int, help="How many times recipients may save it")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="Leave out matching entries (name, path inside the folder, or shown path)")
    parser.add_argument("--allow-flagged", action="store_true",
                        help="Upload secret-looking files anyway (only after the user confirms)")
    parser.add_argument("--lang", choices=("en", "zh"), default="en", help="Language of the message text")
    parser.add_argument("--no-wait", action="store_true",
                        help="Don't wait for PikPak to finish processing uploads")
    parser.add_argument("--dry-run", action="store_true", help="Check and plan only; upload nothing")
    args = parser.parse_args(argv)
    if not args.paths and not args.ids and not args.clouds:
        parser.error("give at least one local path, --cloud PATH or --id")
    if args.expire_days == 0 or args.expire_days < -1:
        parser.error("--expire-days must be -1 (never) or a positive number of days")
    if args.max_restores is not None and args.max_restores < 1:
        parser.error("--max-restores must be at least 1")
    args.exclude = [p.rstrip("/") for p in args.exclude if p.strip("/")]
    return args


def drop(args, state):
    if shutil.which("pikpak") is None:
        raise DropError(4, "NOT_INSTALLED", "The pikpak CLI is not on PATH.",
                        hint="Install it (see the skill's Setup section), then re-run.", stage="preflight",
                        details={"setup": setup_details(installed=False)})

    # 1. Local checks first: no network, no mutation.
    local_items = []
    for raw in args.paths:
        src = Path(raw).expanduser()
        if not src.exists():
            raise DropError(2, "PATH_NOT_FOUND", f"No such file or folder: {raw}", stage="preflight")
        item = scan_local(src.resolve(), args.exclude, args.allow_flagged)
        state["excluded"].extend(item["excluded"])
        if item["files"] == 0:
            raise DropError(2, "NOTHING_TO_UPLOAD", f"{item['name']} has no files left to upload.",
                            hint="Check --exclude patterns.", stage="preflight")
        local_items.append(item)
    flagged = [f for item in local_items for f in item["flagged"]]
    if flagged and not args.allow_flagged:
        raise DropError(3, "FLAGGED_FILES",
                        f"{len(flagged)} item(s) look like secrets or unintended content; nothing was uploaded.",
                        hint="Ask the user. To leave them out re-run with --exclude <path> for each; "
                             "use --allow-flagged only if the user confirms sending them.",
                        stage="preflight", details={"flagged": flagged})

    total_files = sum(i["files"] for i in local_items)
    total_bytes = sum(i["bytes"] for i in local_items)
    if local_items:
        log(f"Checked {total_files} file(s), {human_size(total_bytes)}.")

    # 2. Account checks (read-only).
    check_account(total_bytes)
    cloud_items = []
    for file_id in args.ids:
        info = run_pikpak(["get", file_id, "-F", "json"], stage="preflight")
        cloud_items.append({"source": f"pikpak:{file_id}", "name": info.get("name", file_id),
                            "kind": info.get("type", "file"), "id": file_id})
    for cloud_path in args.clouds:
        cloud_items.append(resolve_cloud_path(cloud_path))

    names = [i["name"] for i in local_items] + [i["name"] for i in cloud_items]
    title = args.title or default_title(names, args.lang)
    password_mode = "none" if args.public else ("custom" if args.password else "random")
    if args.folder is not None:
        folder_path = args.folder.strip("/")
    else:
        label = re.sub(r'[\\/:*?"<>|]+', " ", title).strip()[:60]
        folder_path = f"{DEFAULT_FOLDER}/{time.strftime('%Y-%m-%d %H-%M-%S')} {label}".rstrip()

    if args.dry_run:
        folder_id, _ = resolve_folder(folder_path, create=False) if local_items else (None, False)
        return {
            "ok": True,
            "dry_run": True,
            "plan": {
                "folder": {"path": folder_path, "exists": folder_id is not None} if local_items else None,
                "items": [{k: i[k] for k in ("source", "name", "kind", "files", "bytes")} for i in local_items]
                + [{k: i[k] for k in ("source", "name", "kind")} for i in cloud_items],
                "excluded": state["excluded"],
                "flagged_allowed": flagged,
                "totals": {"files": total_files, "bytes": total_bytes, "size": human_size(total_bytes)},
                "share": {"title": title, "expire_days": args.expire_days, "password": password_mode,
                          "max_restores": args.max_restores},
            },
        }

    # 3. Upload.
    folder_id = None
    if local_items:
        folder_id, folder_created = resolve_folder(folder_path, create=True)
        state["folder"] = {"path": folder_path, "id": folder_id, "created": folder_created}
        tmp_root = Path(tempfile.mkdtemp(prefix="pikpak-drop-"))
        try:
            for item in local_items:
                path = Path(item["source"])
                if item["kind"] == "folder" and (item["excluded"] or item["skipped"]):
                    path = stage_folder(item, tmp_root)
                log(f"Uploading {item['name']} ({human_size(item['bytes'])}) to PikPak/{folder_path} ...")
                cmd = ["upload", str(path), "-F", "json"] + (["-p", folder_id] if folder_id else [])
                if not args.no_wait:
                    cmd.append("-w")
                res = run_pikpak(cmd, stage="upload", retry=False)
                if item["kind"] == "folder":
                    if res.get("failed"):
                        raise DropError(1, "PARTIAL_UPLOAD",
                                        f"{res['failed']} of {res.get('total')} files in {item['name']} failed to upload.",
                                        hint="Re-running resumes unfinished uploads, but creates a second copy of "
                                             "the folder; or share what arrived with `pikpak share create "
                                             f"{res.get('root_folder_id')}`.",
                                        stage="upload", details={"result": res})
                    uploaded = {"id": res["root_folder_id"], "instant_upload": None}
                else:
                    uploaded = {"id": res["file_id"], "instant_upload": res.get("instant_upload")}
                    if res.get("name"):
                        item["name"] = res["name"]  # PikPak renames on collision, e.g. report(1).pdf
                state["items"].append({"source": item["source"], "name": item["name"], "kind": item["kind"],
                                       "files": item["files"], "bytes": item["bytes"], **uploaded})
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)
    state["items"].extend(cloud_items)

    # 4. Share everything under one link.
    ids = [i["id"] for i in state["items"]]
    log("Creating share link ...")
    cmd = ["share", "create", *ids, "-t", title, "-e", str(args.expire_days), "-F", "json"]
    if args.password:
        cmd += ["-p", args.password]
    elif not args.public:
        cmd.append("-P")
    if args.max_restores is not None:
        cmd += ["-r", str(args.max_restores)]
    share = run_pikpak(cmd, stage="share")
    password = share.get("pass_code") or None
    message = build_message(title, share["share_url"], password, args.expire_days, args.lang)
    log(f"Shared: {share['share_url']}")

    return {
        "ok": True,
        "message": message,
        "share": {
            "share_id": share["share_id"],
            "url": share["share_url"],
            "password": password,
            "expire_days": args.expire_days,
            "title": title,
            "max_restores": args.max_restores,
        },
        "folder": state.get("folder"),
        "items": state["items"],
        "excluded": state["excluded"],
        "totals": {"files": total_files, "bytes": total_bytes, "size": human_size(total_bytes)},
        "revoke": f"pikpak share rm {share['share_id']}",
    }


def main(argv=None):
    args = parse_args(argv)
    state = {"items": [], "excluded": [], "folder": None}
    try:
        emit(drop(args, state))
        return 0
    except DropError as exc:
        emit({"ok": False, "error": exc.to_dict(), "folder": state["folder"], "items": state["items"],
              "excluded": state["excluded"]})
        return exc.exit_code
    except KeyboardInterrupt:
        emit({"ok": False, "error": {"code": "INTERRUPTED", "message": "Interrupted."},
              "folder": state["folder"], "items": state["items"], "excluded": state["excluded"]})
        return 1


if __name__ == "__main__":
    sys.exit(main())
