#  SPDX-FileCopyrightText: 2026 Damon Lynch <damonlynch@gmail.com>
#  SPDX-License-Identifier: GPL-3.0-or-later


import contextlib
import hashlib
import json
import locale
import logging
import re
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import track

from extract_raw_pixls.config import repository_directories
from extract_raw_pixls.utilities import format_bytes

# Sets the locale to the user's default system setting
locale.setlocale(locale.LC_ALL, "")

console = Console()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True)],
)

logger = logging.getLogger(__name__)

REPOSITORY = "https://raw.pixls.us/data.lfs.git"
DOMAIN = urlsplit(REPOSITORY).hostname

try:
    config = repository_directories()
except (FileNotFoundError, KeyError, ValueError):
    logger.exception("Could not load valid repository directories")
    sys.exit(1)

GIT_DIRECTORY = config.GIT_DIRECTORY
OUTPUT_DIRECTORY = config.RAW_FILES
MANIFEST_FILE = OUTPUT_DIRECTORY / ".manifest.json"

INVALID_CHARS = re.compile(r'[<>:"/\\|?*]')

RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def validate_directories() -> None:
    """
    Validate that required directories exist, creating them if necessary.

    Checks if GIT_DIRECTORY and OUTPUT_DIRECTORY exist. Creates them if they
    don't exist and exits with error code 1 if creation fails.

    :raises SystemExit: If directory creation fails due to OS errors.
    """
    if not GIT_DIRECTORY.is_dir():
        try:
            GIT_DIRECTORY.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created Git directory {GIT_DIRECTORY}")
        except OSError:
            logger.exception("Could not create Git directory")
            sys.exit(1)
    if not OUTPUT_DIRECTORY.is_dir():
        try:
            OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
            logger.info(f"Created raw files directory {OUTPUT_DIRECTORY}")
        except OSError:
            logger.exception("Could not create raw files directory")
            sys.exit(1)

    logger.info("Git directory: %s", GIT_DIRECTORY)
    logger.info("RAW files directory: %s", OUTPUT_DIRECTORY)


def run(*args: str, cwd: Path | str | None = None) -> str:
    """
    Execute a command and return its stdout output.

    :param args: Command and arguments to execute.
    :param cwd:  Working directory for the command.
    :return: The command's stdout output.
    :raises subprocess.CalledProcessError: If the command returns non-zero exit code.
    """
    return subprocess.check_output(
        args,
        cwd=cwd,
        text=True,
    )


def git(*args: str) -> str:
    """
    Run a git command within the repository directory.

    :param args: Git subcommand and arguments.
    :return: The command's stdout output.
    :raises subprocess.CalledProcessError: If the git command fails.
    """
    return run("git", "-C", str(GIT_DIRECTORY), *args)


def update_repository() -> None:
    """
    Clone or fetch the Git repository and its LFS objects.

    Clones the repository if it doesn't exist, otherwise fetches updates.
    Also fetches all Git LFS objects required for file extraction.

    :raises FileNotFoundError: If git command is not found in PATH.
    :raises subprocess.CalledProcessError: If any git command fails.
    """
    logger.info("Working with Git repository at %s", DOMAIN)

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        if not GIT_DIRECTORY.is_dir():
            logger.info("Cloning Git repository")
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-checkout",
                    REPOSITORY,
                    str(GIT_DIRECTORY),
                ],
                check=True,
                creationflags=creationflags,
            )
        else:
            logger.info("Fetching Git repository changes")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(GIT_DIRECTORY),
                    "fetch",
                    "--all",
                ],
                check=True,
                creationflags=creationflags,
            )

        logger.info("Fetching Git large files (this may take a while)")
        subprocess.run(
            [
                "git",
                "-C",
                str(GIT_DIRECTORY),
                "lfs",
                "fetch",
                "--all",
            ],
            check=True,
            creationflags=creationflags,
        )
    except FileNotFoundError as e:
        logger.error(
            "Git command not found. Please ensure Git is installed and in PATH."
        )
        raise
    except subprocess.CalledProcessError as e:
        logger.error("Git command failed with exit code %d: %s", e.returncode, e.cmd)
        raise


def sanitize_component(name: str) -> str:
    """
    Sanitize a path component for safe filesystem usage.

    Removes invalid characters, strips trailing spaces/dots, and prefixes
    reserved Windows filenames to prevent conflicts.

    :param name: The path component to sanitize.
    :return: A sanitized string safe for use as a filename or directory name.
    """
    result = INVALID_CHARS.sub("-", name)
    result = result.rstrip(" .")

    if not result:
        result = "_"

    if result.split(".")[0].upper() in RESERVED_NAMES:
        result = "_" + result

    return result


def sanitize_path(path: str, oid: str) -> Path:
    """
    Sanitize a full path for safe filesystem usage.

    Processes each path component through sanitize_component and appends
    a hash digest to the filename if sanitization altered the original path,
    preventing collisions.

    :param path: The original POSIX path to sanitize.
    :param oid: Object identifier used for collision resolution hashing.
    :return: A Path object with sanitized components.
    """
    parts = [sanitize_component(p) for p in PurePosixPath(path).parts]

    result = Path(*parts)

    # Prevent collisions after sanitizing
    if str(result) != path:
        digest = hashlib.sha1(oid.encode()).hexdigest()[:8]
        result = result.with_name(f"{result.stem}_{digest}{result.suffix}")

    return result


def load_manifest() -> dict[str, dict[str, str]]:
    """
    Load the extraction manifest from disk.

    Reads and parses the JSON manifest file. Returns an empty dictionary
    if the file doesn't exist or is corrupted.

    :return: Dictionary mapping file paths to their SHA and destination paths.
    """
    if MANIFEST_FILE.exists():
        try:
            return json.loads(MANIFEST_FILE.read_text("utf-8"))
        except json.JSONDecodeError as e:
            logger.warning("Corrupted manifest file, starting fresh: %s", e)
    return {}


def save_manifest(manifest: dict[str, dict[str, str]]) -> None:
    """
    Save the extraction manifest to disk atomically.

    Writes the manifest to a temporary file first, then atomically replaces
    the existing manifest to prevent corruption on interruption.

    :param manifest: Dictionary mapping file paths to their SHA and destination paths.
    """
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_FILE.with_suffix(".manifest.json.tmp")
    tmp.write_text(
        json.dumps(
            manifest,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    tmp.replace(MANIFEST_FILE)


def get_tree() -> list[dict[str, str]]:
    """
    Get all blob entries from the Git repository tree.

    Uses git ls-tree to list all files in the repository, filtering out
    directories, symlinks, and submodules.

    :return: List of dictionaries with 'sha' and 'path' keys for each blob.
    """
    output = git(
        "ls-tree",
        "-r",
        "-z",
        "HEAD",
    )

    entries = []

    for item in output.split("\0"):
        if not item:
            continue

        mode, typ, sha, path = item.split(maxsplit=3)

        # Skip trees (directories), symlinks, and gitlink (submodules)
        if typ != "blob":
            logger.debug("Skipping non-blob entry: %s (%s)", path, typ)
            continue

        entries.append(
            {
                "sha": sha,
                "path": path,
            }
        )

    return entries


def read_blob(sha: str) -> bytes:
    """
    Read the raw content of a Git blob.
        
    :param sha: The SHA hash of the blob to read.
    :return: The raw bytes content of the blob.
    :raises subprocess.CalledProcessError: If the git command fails.
    """
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(GIT_DIRECTORY),
            "cat-file",
            "blob",
            sha,
        ]
    )


def find_lfs_object(oid: str) -> Path:
    """
    Find the local path of a Git LFS object by its OID.
        
    :param oid: The SHA256 OID of the LFS object.
    :return: Path to the LFS object file.
    :raises FileNotFoundError: If the LFS object doesn't exist locally.
    """
    path = GIT_DIRECTORY / ".git" / "lfs" / "objects" / oid[0:2] / oid[2:4] / oid
    if not path.exists():
        raise FileNotFoundError(
            f"LFS object {oid} not found. Ensure 'git lfs fetch --all' completed."
        )
    return path


def get_file_source(entry: dict[str, str]) -> Path | bytes:
    """
    Get the source for a file entry, handling both regular and LFS files.
    
    For LFS pointer files, returns the path to the actual LFS object.
    For regular files, returns the raw content bytes.

    :param entry: Dictionary with 'sha' and 'path' keys from git ls-tree.
    :return: Path to LFS object or bytes content of regular file.
    :raises RuntimeError: If LFS pointer file is malformed.
    :raises subprocess.CalledProcessError: If git command fails.
    """
    data = read_blob(entry["sha"])

    if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
        text = data.decode()

        for line in text.splitlines():
            if line.startswith("oid sha256:"):
                oid = line.split(":")[1]
                return find_lfs_object(oid)

        raise RuntimeError(f"Invalid LFS pointer: {entry['path']}")

    return data


def extract(dry_run: bool = False) -> None:
    """
    Extract files from the Git repository to the output directory.
    
    Compares current repository state with the existing manifest to determine
    which files need to be added, updated, or removed. Updates the manifest
    atomically when complete.
    
    :param dry_run: If True, only report what changes would be made to the RAW files 
       without actually modifying the filesystem.
    """
    if dry_run:
        logger.info("=== DRY RUN MODE - No changes will be made ===")
    else:
        OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)

    old_manifest = load_manifest()
    new_manifest = {}

    tree = get_tree()

    files_to_add = 0
    files_to_update = 0
    files_to_remove = 0

    for entry in track(tree, description="Extracting", console=console):
        source = get_file_source(entry)

        destination = OUTPUT_DIRECTORY / sanitize_path(
            entry["path"],
            entry["sha"],
        )

        key = entry["path"]

        new_manifest[key] = {
            "sha": entry["sha"],
            "destination": str(destination),
        }

        if old_manifest.get(key) == new_manifest[key]:
            continue

        if isinstance(source, Path):
            source_size = (
                format_bytes(source.stat().st_size)
                if source.exists()
                else format_bytes(0)
            )
        else:
            source_size = format_bytes(len(source))

        if dry_run:
            if key in old_manifest:
                logger.info(
                    "  UPDATE: %s -> %s (%s)", entry["path"], destination, source_size
                )
                files_to_update += 1
            else:
                logger.info(
                    "  ADD:    %s -> %s (%s)", entry["path"], destination, source_size
                )
                files_to_add += 1
        else:
            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if isinstance(source, Path):
                shutil.copy2(
                    source,
                    destination,
                )
            else:
                destination.write_bytes(source)

    removed = set(old_manifest) - set(new_manifest)

    for item in removed:
        old_file = Path(old_manifest[item]["destination"])
        if dry_run:
            if old_file.exists():
                logger.info(f"  REMOVE: {old_file}")
                files_to_remove += 1
        else:
            try:
                old_file.unlink(missing_ok=True)
            except OSError as e:
                logger.warning("Failed to remove %s: %s", old_file, e)

    if not dry_run:
        save_manifest(new_manifest)

        # Clean up empty directories left behind
        for dirpath in sorted(OUTPUT_DIRECTORY.rglob("*"), reverse=True):
            if dirpath.is_dir():
                with contextlib.suppress(OSError):
                    dirpath.rmdir()  # Only succeeds if empty

    else:
        logger.info("=== DRY RUN SUMMARY ===")
        logger.info(
            f"Files to add:    {locale.format_string('%d', files_to_add, grouping=True)}"
        )
        logger.info(
            f"Files to update: {locale.format_string('%d', files_to_update, grouping=True)}"
        )
        logger.info(
            f"Files to remove: {locale.format_string('%d', files_to_remove, grouping=True)}"
        )
        logger.info(
            f"Total changes:   {locale.format_string('%d', files_to_add + files_to_update + files_to_remove, grouping=True)}"
        )


def main() -> None:
    """Main entry point for the extraction script.

    Parses command line arguments, validates directories, updates the
    repository, and extracts files.
    """
    parser = ArgumentParser(
        description=f"Extract RAW files from Git LFS repository at {DOMAIN}"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes to RAW files without modifying the filesystem",
    )
    args = parser.parse_args()
    validate_directories()
    update_repository()
    extract(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
