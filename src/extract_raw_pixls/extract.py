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


def run(*args: str, cwd: Path | str | None = None, text:bool=True) -> str:
    """
    Execute a command and return its stdout output.

    :param args: Command and arguments to execute.
    :param cwd:  Working directory for the command.
    :param text: subprocess.check_output text mode
    :return: The command's stdout output.
    :raises subprocess.CalledProcessError: If the command returns non-zero exit code.
    """
    return subprocess.check_output(
        args,
        cwd=cwd,
        text=text,
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

    # Windows device names are reserved even when an extension is present.
    if result.split(".", 1)[0].upper() in RESERVED_NAMES:
        result = "_" + result

    return result

def sanitize_path(path: str) -> Path:
    """Sanitize a Git path without adding a collision suffix."""
    return Path(
        *(sanitize_component(part)
          for part in PurePosixPath(path).parts)
    )


def make_destination_map(entries):
    """
    Map every Git path to a unique Windows-safe destination.

    A hash is added only when two or more Git paths would produce
    the same Windows filename on Windows.
    """
    candidates = {}

    for entry in entries:
        destination = sanitize_path(entry["path"])
        key = str(destination).casefold()

        candidates.setdefault(key, []).append(
            (entry, destination)
        )

    result = {}

    for matching_entries in candidates.values():
        if len(matching_entries) == 1:
            entry, destination = matching_entries[0]
            result[entry["path"]] = destination
            continue

        # Genuine collision after Windows filename sanitisation.
        for entry, destination in matching_entries:
            digest = hashlib.sha256(
                entry["sha"].encode("ascii")
            ).hexdigest()[:8]

            result[entry["path"]] = destination.with_name(
                f"{destination.stem}_{digest}{destination.suffix}"
            )

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

    output = subprocess.check_output(
        [
            "git",
            "-C",
            str(GIT_DIRECTORY),
            "-c",
            "core.quotepath=false",
            "ls-tree",
            "-r",
            "-z",
            "HEAD",
        ]
    )

    entries = []

    for item in output.split(b"\0"):
        if not item:
            continue

        metadata, path = item.split(b"\t", 1)

        mode, typ, sha = metadata.split(b" ", 2)

        entries.append(
            {
                "sha": sha.decode("ascii"),
                "path": path.decode("utf-8"),
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
    Extract all files from the Git repository into the output directory.

    Compares the current repository tree against the saved manifest and
    copies or writes any changed files, resolving Git LFS pointers to the
    locally fetched LFS objects. Removes physical files that no longer
    exist in the tree or whose destination changed, then saves the new
    manifest.

    With dry_run enabled, prints the files that would be copied or removed
    and summary statistics without modifying the filesystem or manifest.

    :param dry_run: When True, preview changes without modifying the filesystem.
    """
    old_manifest = load_manifest()
    new_manifest = {}

    tree = get_tree()
    destination_map = make_destination_map(tree)

    changed = 0
    unchanged = 0
    collisions = 0

    # Build the new manifest first.
    for entry in tree:
        source_path = entry["path"]

        destination = (
            OUTPUT_DIRECTORY
            / destination_map[source_path]
        )

        new_manifest[source_path] = {
            "sha": entry["sha"],
            "destination": str(destination),
        }

    # Find physical files that need to be removed.
    files_to_remove = set()

    # 1. Git paths that no longer exist.
    for source_path in set(old_manifest) - set(new_manifest):
        files_to_remove.add(
            old_manifest[source_path]["destination"]
        )

    # 2. Existing Git paths whose local destination changed.
    for source_path in set(old_manifest) & set(new_manifest):
        old_destination = old_manifest[source_path]["destination"]
        new_destination = new_manifest[source_path]["destination"]

        if old_destination != new_destination:
            files_to_remove.add(old_destination)

    # Extract/update files.
    for entry in track(tree, description="Extracting"):
        source_path = entry["path"]
        destination = Path(
            new_manifest[source_path]["destination"]
        )

        if old_manifest.get(source_path) == new_manifest[source_path]:
            unchanged += 1
            continue

        changed += 1

        if (
            destination_map[source_path]
            != sanitize_path(source_path)
        ):
            collisions += 1

        if dry_run:
            print(
                f"Would extract:\n"
                f"  {source_path}\n"
                f"  -> {destination}"
            )
            continue

        source = get_file_source(entry)

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if isinstance(source, Path):
            shutil.copy2(source, destination)
        else:
            destination.write_bytes(source)

    # Remove obsolete physical files.
    if dry_run:
        for filename in sorted(files_to_remove):
            print(f"Would remove: {filename}")

        print()
        print(f"Files:        {len(tree):,}")
        print(f"Unchanged:    {unchanged:,}")
        print(f"Would copy:   {changed:,}")
        print(f"Would remove: {len(files_to_remove):,}")
        print(f"Collisions:   {collisions:,}")
        return

    for filename in files_to_remove:
        path = Path(filename)

        if path.exists():
            path.unlink()

    save_manifest(new_manifest)

    print()
    print(f"Files:        {len(tree):,}")
    print(f"Unchanged:    {unchanged:,}")
    print(f"Copied:       {changed:,}")
    print(f"Removed:      {len(files_to_remove):,}")
    print(f"Collisions:   {collisions:,}")


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
