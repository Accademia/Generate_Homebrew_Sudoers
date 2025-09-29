#!/usr/bin/env python3
"""
Script to download and reinstall all Homebrew Cask packages.

This script operates in two stages to provide more control over the
reinstallation process:

1. **Download Stage:** All Cask application bundles are downloaded to
   Homebrew's cache using `brew fetch --cask`.  Downloads are
   performed in parallel to speed up the process.  When the optional
   `tqdm` library is available, a progress bar reflects how many apps
   have been downloaded (e.g., "Downloading casks (3/10)") and shows a
   percentage complete.  If `tqdm` is not installed, the script falls
   back to a simple `Downloaded n/m` counter that updates in place.
   If the script is interrupted and rerun, previously downloaded
   packages are skipped.

2. **Install Stage:** Once all packages have been fetched, the script
   reinstalls them one by one using

       brew reinstall --cask --verbose --debug <cask>

   During installation, all output (including the command itself,
   verbose and debug output, and any error messages) is printed to
   the screen **and** appended to a log file named
   ``reinstall_casks_install.log`` in the current working directory.
   After each successful installation, the script records progress so
   that a later run resumes from where it left off.

To persist progress across runs, a JSON state file is written in the
current working directory.  It keeps track of which casks have been
downloaded and which have been installed.

Note: Running this script will make changes to your system by
reinstalling software.  Ensure that you have the necessary
permissions and that you understand the implications before
executing it.  The download stage can be executed without elevated
privileges, but the install stage may prompt for a password if
Homebrew requires one.
"""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Attempt to import tqdm for fancy progress bars.  If unavailable,
# fall back to a simple console-based progress indicator.
try:
    from tqdm import tqdm  # type: ignore[import]
except ImportError:  # pragma: no cover - handle missing dependency at runtime
    tqdm = None  # type: ignore[assignment]


STATE_FILE = Path("reinstall_casks_state.json")

# Maximum number of casks to download in parallel during the download phase.
# You can adjust this value to control how many concurrent `brew fetch`
# operations are run.  A higher number may speed up downloads on fast
# connections but could consume more resources or lead to network throttling.
MAX_DOWNLOAD_WORKERS = 32


def get_installed_casks() -> List[str]:
    """Return a list of installed Homebrew Cask packages.

    Uses `brew list --cask` to obtain the names of installed casks.

    Raises SystemExit with a nonzero status if `brew` encounters an error or
    is not found.
    """
    try:
        result = subprocess.run(
            ["brew", "list", "--cask"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        print(
            "Error: Homebrew is not installed or 'brew' is not in the PATH.",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(
            f"Error retrieving installed cask packages (exit code {exc.returncode}).\n"
            f"Output: {exc.stderr}",
            file=sys.stderr,
        )
        sys.exit(exc.returncode)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def load_state() -> Tuple[Set[str], Set[str]]:
    """Load download/install progress from the state file.

    Returns a tuple of two sets: (downloaded_casks, installed_casks).
    If the state file does not exist or is invalid, empty sets are returned.
    """
    downloaded: Set[str] = set()
    installed: Set[str] = set()
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                data: Dict[str, List[str]] = json.load(f)
                if isinstance(data, dict):
                    downloaded = set(data.get("downloaded", []))
                    installed = set(data.get("installed", []))
        except Exception:
            # If the file cannot be read or parsed, ignore and start fresh
            print(
                f"Warning: Could not read state file {STATE_FILE}; starting with a fresh state.",
                file=sys.stderr,
            )
    return downloaded, installed


def save_state(downloaded: Set[str], installed: Set[str]) -> None:
    """Persist the download/install progress to the state file."""
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump({"downloaded": sorted(downloaded), "installed": sorted(installed)}, f)
    except Exception as exc:
        print(
            f"Warning: Failed to write state file {STATE_FILE}: {exc}", file=sys.stderr
        )


def fetch_cask(cask: str) -> Tuple[str, bool]:
    """Fetch a single cask's installer using `brew fetch --cask`.

    Returns a tuple of the cask name and a boolean indicating success.
    Output from the fetch command is suppressed to avoid cluttering the
    console.  A non-zero exit code is treated as failure.
    This function is intended to be run in a worker thread.
    """
    try:
        # Suppress output from brew fetch by redirecting stdout and stderr to DEVNULL
        proc = subprocess.run(
            ["brew", "fetch", "--cask", cask],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        success = proc.returncode == 0
        return cask, success
    except Exception:
        return cask, False


def reinstall_cask(cask: str, log_file) -> None:
    """Reinstall a single cask using Homebrew.

    Executes `brew reinstall --cask --verbose --debug <cask>` and streams
    the output to both the console and a provided log file.

    Args:
        cask: The name of the cask to reinstall.
        log_file: A file-like object opened for appending where output
            should be recorded.  Each line printed to the screen will
            also be written to this file.
    """
    # Construct the command
    cmd = ["brew", "reinstall", "--cask", "--verbose", "--debug", cask]
    # Write the command itself to both destinations
    header = f"\nRunning command: {' '.join(cmd)}\n"
    print(header, end="")
    log_file.write(header)
    log_file.flush()
    # Start the process and stream output line by line
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None  # for type checkers
    for line in process.stdout:
        # Print to screen
        print(line, end="")
        # Append to log
        log_file.write(line)
    retcode = process.wait()
    if retcode != 0:
        err_msg = f"Error: Command for '{cask}' exited with return code {retcode}\n"
        print(err_msg, file=sys.stderr, end="")
        log_file.write(err_msg)


def main() -> None:
    """Main function to download and reinstall Homebrew Cask packages.

    This function supports two modes of operation:

    1. **Reinstall all installed casks** (default):
       When no cask names are specified via command‑line arguments or
       the ``CASKS`` environment variable, the script queries ``brew`` to
       determine the full list of currently installed cask packages and
       proceeds to download and reinstall them.

    2. **Reinstall specific casks**: If one or more cask names are
       provided either as positional command‑line arguments or via the
       ``CASKS`` environment variable, only those casks will be
       processed.  This is useful for targeting a subset of installed
       applications without affecting the rest.  When running in this
       mode, the script still maintains its progress state, but only
       operates on the selected casks.  Progress messages (e.g.,
       "Downloading casks" and "Install phase") will accurately reflect
       the number of targeted casks rather than the total number of
       installed casks.

    Regardless of mode, the workflow consists of two phases:

    1. Downloading application bundles using ``brew fetch --cask``.
    2. Reinstalling each package with ``brew reinstall --cask --verbose --debug``.

    Progress for each phase is persisted in a JSON state file so that
    interrupted runs can be resumed.  If the state file exists,
    previously downloaded or installed casks are skipped.
    """

    import os

    # Determine which casks to process.  Priority order:
    #   1. Positional command‑line arguments (excluding the script name).
    #   2. Environment variable CASKS, which may contain a whitespace‑
    #      separated list of cask names.
    #   3. Fallback to all currently installed casks returned by brew.
    specified: List[str] = []
    # Exclude the script name; sys.argv[0] is the script path.
    if len(sys.argv) > 1:
        # Treat all remaining arguments as cask names.  No option parsing
        # is performed; this keeps the interface simple.
        specified = sys.argv[1:]
    elif os.environ.get("CASKS"):
        # Split on whitespace to support multiple names separated by spaces
        specified = os.environ["CASKS"].split()

    if specified:
        # Use only the specified casks.  Do not query brew for the full list.
        casks: List[str] = specified
    else:
        # No casks specified; fallback to querying installed casks.
        casks = get_installed_casks()

    if not casks:
        print("No Homebrew Cask packages found to process.")
        return

    # Load previous state if available
    downloaded, installed = load_state()

    # Phase 1: download any casks that haven't been fetched yet
    # Only consider casks selected for this run when calculating counts.
    to_download = [c for c in casks if c not in downloaded]
    if to_download:
        print(
            f"Starting download phase: {len(to_download)} of {len(casks)} casks need to be fetched."
        )
        # Use a thread pool to download multiple casks concurrently.  Limit
        # the number of parallel downloads based on the configured maximum.
        max_workers = min(MAX_DOWNLOAD_WORKERS, len(to_download))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Map each future to the cask it will fetch
            future_to_cask = {
                executor.submit(fetch_cask, cask): cask for cask in to_download
            }
            if tqdm:
                # If tqdm is available, use it to display a detailed progress bar
                with tqdm(total=len(to_download), desc=f"Downloading casks (0/{len(to_download)})") as pbar:
                    for future in as_completed(future_to_cask):
                        cask_name, success = future.result()
                        pbar.update(1)
                        # Update the description to reflect how many have been downloaded so far
                        pbar.set_description(
                            f"Downloading casks ({pbar.n}/{len(to_download)})"
                        )
                        if success:
                            downloaded.add(cask_name)
                            # Persist the updated state after each successful download
                            save_state(downloaded, installed)
                print("Download phase completed.")
            else:
                # Fallback: simple console progress without tqdm
                total_tasks = len(to_download)
                completed_tasks = 0
                # Print initial progress
                print(f"Downloading casks: {completed_tasks}/{total_tasks}", end="", flush=True)
                for future in as_completed(future_to_cask):
                    cask_name, success = future.result()
                    completed_tasks += 1
                    # Move cursor to beginning of line and print updated count
                    print(
                        f"\rDownloading casks: {completed_tasks}/{total_tasks}",
                        end="",
                        flush=True,
                    )
                    if success:
                        downloaded.add(cask_name)
                        save_state(downloaded, installed)
                # Ensure the progress line ends cleanly
                print()
                print("Download phase completed.")
    else:
        # No casks to download
        print("All casks have already been downloaded; skipping download phase.")

    # Phase 2: reinstall any casks that haven't been installed yet
    to_install = [c for c in casks if c not in installed]
    if to_install:
        print(
            f"Starting install phase: {len(to_install)} of {len(casks)} casks need to be reinstalled."
        )
        # Open a log file for appending installation output
        log_path = Path("reinstall_casks_install.log")
        with log_path.open("a", encoding="utf-8") as log_file:
            for cask_name in to_install:
                reinstall_cask(cask_name, log_file)
                installed.add(cask_name)
                save_state(downloaded, installed)
        print("Install phase completed.")
        print(f"Installation logs have been saved to {log_path.resolve()}")
    else:
        print("All casks have already been reinstalled; nothing left to do.")


if __name__ == "__main__":
    main()
