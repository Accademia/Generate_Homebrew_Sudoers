#!/usr/bin/env python3
"""
Generate sudoers rules for Homebrew casks with version‑agnostic wildcarding.

This script inspects installed Homebrew casks (or a list specified via the
`CASKS` environment variable), extracts privileged actions from each cask's
metadata, and emits a sudoers snippet permitting those actions without a
password.  It supports both the legacy JSON format returned by older
`brew info --cask --json=v2` invocations and the newer format where the
top‑level object contains a `"casks"` list.  The script will also
attempt to fetch metadata from the Homebrew formulae API if local brew
information is unavailable.

Key features:

  * Recursively flattens `artifacts` entries and normalises two‑element
    arrays (e.g. `['app','Foo.app']`) into dictionaries.
  * Wildcards version numbers, dates, team IDs and other variable
    substrings in application paths, package names, script names,
    pkgutil identifiers, launchctl labels and deletion paths so that
    sudoers rules remain valid across version upgrades.
  * Adds rules permitting Homebrew's internal write‑test (`touch
    <App>.app/.homebrew-write-test`) and subsequent removal of the test
    file, as well as removal of the `Contents` directory when brew
    performs an in‑place overwrite.
  * Generates rules for both the system Swift binary and the Command
    Line Tools Swift binary to copy extended attributes.
  * Produces a single sudoers snippet for the specified user and writes
    it to `homebrew-cask.nopasswd.sudoers` (or a path specified via
    `SUDOERS_OUT`).

Usage examples:

    # Generate sudoers for all installed casks
    python3 gen_brew_cask_sudoers.py

    # Only generate rules for chatgpt and adguard
    CASKS="chatgpt adguard" python3 gen_brew_cask_sudoers.py

    # Specify sudoers output file and target user
    TARGET_USER=你的用户名 SUDOERS_OUT=/tmp/cask_sudoers python3 gen_brew_cask_sudoers.py

"""

import json
import os
import re
import subprocess
import sys
import shutil
import shlex
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ----------------------------------------------------------------------
# Utility functions

def run(cmd: Iterable[str]) -> str:
    """Execute a command and return its stripped stdout.  Raises on error."""
    return subprocess.check_output(list(cmd), text=True).strip()


def fetch_cask_json(token: str) -> Optional[Dict[str, Any]]:
    """Return the cask metadata dict for `token`, or None on failure.

    First attempts to call `brew info --cask --json=v2` and parse the
    result.  If that fails, falls back to the formulae.brew.sh API.
    Supports both legacy JSON (list or dict) and new JSON with a
    top‑level "casks" array.
    """
    # Try local brew first
    try:
        out = run(["brew", "info", "--cask", "--json=v2", token])
        data = json.loads(out)
        # New format: top‑level dict containing "casks"
        if isinstance(data, dict) and "casks" in data:
            return data["casks"][0] if data["casks"] else None
        # Legacy: list of one element
        if isinstance(data, list) and data:
            return data[0]
        # Legacy: single dict
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Fallback to formulae API
    url = f"https://formulae.brew.sh/api/cask/{token}.json"
    try:
        with urllib.request.urlopen(url) as fh:
            return json.load(fh)
    except Exception:
        return None


def parse_artifacts(cj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Recursively flatten the `artifacts` array from a cask JSON.

    Returns a list of dictionaries, converting any two‑element list
    entries like `['app', 'Foo.app']` into `{'app': ['Foo.app']}`.  Any
    nested lists are flattened.  Non‑dict, non‑list entries are ignored.
    """
    raw = cj.get("artifacts")
    flattened: List[Dict[str, Any]] = []
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node:
                flattened.append(node)
            return
        if isinstance(node, list):
            # Two‑element list encoding: [key, value] where value may be list
            if len(node) == 2 and isinstance(node[0], str):
                key = node[0]
                val = node[1]
                flattened.append({key: [val] if not isinstance(val, list) else val})
                return
            for item in node:
                walk(item)
            return
        # Ignore scalars
    walk(raw)
    return flattened


def ensure_list(x: Any) -> List[Any]:
    """Return `x` as a list: scalar -> [x], list -> x, None -> []."""
    if x is None:
        return []
    if isinstance(x, list):
        return x
    return [x]


def sudo_escape(s: str) -> str:
    """Escape spaces and colons for sudoers."""
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace(":", "\\:")


def join_command(cmd: str, args: List[str], allow_trailing_star: bool = False) -> str:
    """Join command and args with sudo escaping.  Optionally append a trailing *."""
    parts = [sudo_escape(cmd)] + [sudo_escape(a) for a in args]
    out = " ".join(parts)
    if allow_trailing_star:
        out += " *"
    return out


###############################################################################
# Log parsing helpers

def _find_log_command_tokens(tokens: List[str]) -> Optional[Tuple[str, List[str]]]:
    """Given a shlex‑split line, find the real command path and args after sudo.

    Skips environment assignments and sudo flags (e.g. -E, -u root, PATH=...).
    Returns (cmd, args) or None if not found.
    """
    try:
        idx = tokens.index('/usr/bin/sudo') + 1
    except ValueError:
        return None
    # Skip sudo flags like -u root -E and PATH= assignments
    while idx < len(tokens):
        tok = tokens[idx]
        # environment assignment (contains '=' but not starting with '/')
        if '=' in tok and not tok.startswith('/'):
            idx += 1
            continue
        # skip sudo options (e.g. -E, -n, -u, --)
        if tok in ('--'):
            idx += 1
            continue
        if tok.startswith('-'):
            # if -u, skip next token (username)
            if tok == '-u' and idx + 1 < len(tokens):
                idx += 2
                continue
            idx += 1
            continue
        break
    if idx >= len(tokens):
        return None
    cmd = tokens[idx]
    args = tokens[idx + 1:]
    return (cmd, args)


def _normalize_log_command(cmd: str, args: List[str], user: str, brew_prefix: str) -> Optional[str]:
    """Return a sudoers rule for a log‑derived command.

    Applies minimal wildcarding: chown user part to '*:staff'; wildcard
    version directories in Caskroom paths; unify .app paths; wildcard
    deletion targets; handle common installer/cp/touch patterns.

    Before performing any normalisation, resolve bare command names to
    absolute paths.  The reinstall logs sometimes record commands like
    ``rm`` or ``touch`` without a leading slash.  Sudoers requires
    absolute paths, so we attempt to resolve such bare commands using
    :func:`shutil.which` and a fallback search in common system
    directories.  If the command cannot be resolved to an existing
    executable, the log line is ignored (returns ``None``).
    """
    # Ensure bare command names are resolved to absolute paths.  If cmd
    # contains no slash, try to locate it using shutil.which.  On
    # failure, fallback to scanning a list of common directories.
    if cmd and not cmd.startswith('/'):
        # Use shutil.which to honour PATH; may return None.
        abs_cmd = shutil.which(cmd)
        if abs_cmd:
            cmd = abs_cmd
        else:
            # Fallback search in typical system directories.
            for search_dir in ('/usr/bin', '/bin', '/usr/sbin', '/sbin'):
                candidate = os.path.join(search_dir, cmd)
                if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                    cmd = candidate
                    break
            else:
                # Unresolvable command; skip creating a rule.
                return None
    # Skip obvious noise or empty commands.  Some log lines contain messages
    # enclosed in backticks or have no real command; ignore those.
    if not cmd or '`' in cmd:
        return None
    # Normalise chown: replace user:group with '*:staff'.  Handle both
    # /usr/sbin/chown and /bin/chown variants (or any absolute chown path).
    if os.path.basename(cmd) == 'chown' and args:
        # Expect pattern: -R, '--', user:group, path...
        # We want to change the first non‑flag argument that contains ':'
        new_args = []
        replaced = False
        for a in args:
            if not replaced and ':' in a and not a.startswith('-'):
                parts = a.split(':')
                if len(parts) == 2 and parts[1] == 'staff':
                    new_args.append('*:staff')
                    replaced = True
                    continue
            new_args.append(a)
        args = new_args
    # Normalise cp: unify Caskroom version directory for the source and
    # preserve flags and the destination.  The cp command in logs
    # typically has the form ``cp [options] <source> <dest>``.  We
    # identify the source as the penultimate argument and the
    # destination as the last argument, leaving any flags untouched.
    if os.path.basename(cmd) == 'cp' and len(args) >= 2:
        # Determine source and destination as the last two arguments
        src = args[-2]
        dest = args[-1]
        new_src = src
        # If the source is under the Caskroom, wildcard the version
        # directory.  Example: /opt/homebrew/Caskroom/ares-emulator/146/ares-v146/ares.app
        if isinstance(src, str) and src.startswith(brew_prefix) and '/Caskroom/' in src:
            parts = src.split('/')
            try:
                idx = parts.index('Caskroom')
                # Replace the version directory with '*'
                if idx + 2 < len(parts):
                    parts[idx + 2] = '*'
                new_src = '/'.join(parts)
            except ValueError:
                new_src = src
        # Additionally, wildcard numeric suffixes in the app bundle name of the
        # source path (e.g. 'Alfred 5.app' -> 'Alfred *.app').  This makes
        # cp rules derived from logs version‑agnostic for the source as well.
        if isinstance(new_src, str):
            new_src = _wildcard_app_path(new_src)
        # Destination: do not alter case or introduce wildcards.  Leave
        # dest unchanged because sudoers must match the actual path,
        # including case (e.g. qBittorrent.app vs qbittorrent.app).
        # Apply wildcarding to version numbers in the destination app bundle
        # (e.g. 'AirParrot 3.app' -> 'AirParrot*.app'), while preserving
        # case and other characters.  Use _wildcard_app_path to generalise
        # numeric suffixes that follow spaces, hyphens or underscores.
        new_dest = _wildcard_app_path(dest) if isinstance(dest, str) else dest
        # Build a new arguments list preserving flags and replacing src/dest
        new_args = list(args)
        new_args[-2] = new_src
        new_args[-1] = new_dest
        return f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(cmd, new_args)
    # Normalise touch of .homebrew-write-test: allow a generic rule.  If the
    # command basename is 'touch', always generate the generic rule.
    if os.path.basename(cmd) == 'touch':
        # Accept any .homebrew-write-test touch
        return f"{user} ALL=(ALL) NOPASSWD: SETENV: /usr/bin/touch /Applications/*.app/.homebrew-write-test"
    # Normalise rmdir and rm paths.  Handle any absolute rmdir path.
    if os.path.basename(cmd) == 'rmdir' and args:
        path = args[-1]
        # unify path using wildcard delete
        new_path = _wildcard_delete_path(path)
        return f"{user} ALL=(ALL) NOPASSWD: SETENV: /bin/rmdir -- {new_path}"
    if os.path.basename(cmd) == 'rm' and args:
        # For rm commands, preserve any existing option flags (e.g. -f, -r)
        # and only wildcard the final path argument.  We do not insert
        # additional flags here because the sudoers entry must match the
        # command invocation's arguments to be effective.  For example,
        # a log line like ``rm /Applications/foo.app/.homebrew-write-test``
        # will produce a rule ``/usr/bin/rm /Applications/foo.app/.homebrew-write-test``,
        # whereas ``rm -f -- /Library/LaunchDaemons/com.foo.plist`` will
        # result in ``/usr/bin/rm -f -- /Library/LaunchDaemons/com.foo.plist``.
        new_args = list(args)
        # Replace the last argument with a wildcarded path.
        new_args[-1] = _wildcard_delete_path(new_args[-1])
        return f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(cmd, new_args)
    # Normalise installer commands.  Handle both /usr/sbin/installer and
    # /usr/bin/installer variants (basename == 'installer').  We collapse
    # any target specification and verbose flags into a single wildcard
    # argument after '-target', and wildcard version/hashes in any
    # choice XML paths.  Simplify installer invocations into a single
    # rule that collapses the target and verbose flags into one
    # `-target*` token.  This avoids generating separate verbose and
    # non‑verbose patterns and prevents the production of multiple
    # adjacent wildcard arguments.
    if os.path.basename(cmd) == 'installer' and '-pkg' in args:
        """
        Normalise installer invocations from the logs.

        Homebrew invokes the macOS `installer` binary with a `-pkg`
        argument followed by a mandatory `-target /` and, if run in
        verbose mode, one or more `-verbose*` flags.  We collapse the
        target and verbose options into a single wildcarded token
        (`-target*`) so that a single sudoers entry will match both
        verbose and non‑verbose forms.  The package path has its
        version directory wildcarded and its filename digit/hashes
        replaced via `_wildcard_pkg_name`.  Any `-applyChoiceChangesXML`
        argument has its filename replaced with a generic
        `choices*.xml` to avoid embedding dates or random strings.
        """
        pkg_path_w: Optional[str] = None
        xml_path_w: Optional[str] = None
        other_flags: List[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            if a == '-pkg' and i + 1 < len(args):
                pkg_path = args[i + 1]
                # Wildcard version directory in Caskroom path
                if isinstance(pkg_path, str) and pkg_path.startswith(brew_prefix) and '/Caskroom/' in pkg_path:
                    parts = pkg_path.split('/')
                    try:
                        idx = parts.index('Caskroom')
                        if idx + 2 < len(parts):
                            parts[idx + 2] = '*'
                        pkg_path = '/'.join(parts)
                    except ValueError:
                        pass
                # Separate directory and filename, wildcard filename version
                pkg_dir, pkg_file = os.path.split(pkg_path)
                pkg_file_w = _wildcard_pkg_name(pkg_file)
                pkg_path_w = os.path.join(pkg_dir, pkg_file_w)
                i += 2
                continue
            # Skip '-target' and its argument if present; also skip any
            # subsequent '-verbose*' flags.
            if a == '-target':
                i += 1
                if i < len(args) and not args[i].startswith('-'):
                    i += 1
                while i < len(args) and args[i].startswith('-verbose'):
                    i += 1
                continue
            # Replace applyChoiceChangesXML file with choices*.xml
            if a == '-applyChoiceChangesXML' and i + 1 < len(args):
                xml_path = args[i + 1]
                try:
                    xml_dir, xml_file = os.path.split(xml_path)
                    if xml_file.startswith('choices'):
                        xml_path_w = os.path.join(xml_dir, 'choices*.xml')
                    else:
                        xml_path_w = os.path.join(xml_dir, '*.xml')
                except Exception:
                    xml_path_w = '/private/tmp/choices*.xml'
                i += 2
                continue
            # Skip any standalone verbose flags
            if a.startswith('-verbose'):
                i += 1
                continue
            # Preserve any other flags
            other_flags.append(a)
            i += 1
        if not pkg_path_w:
            return None
        # Build argument list: -pkg <path> + other flags + '-target*'
        args_list: List[str] = []
        args_list.extend(['-pkg', pkg_path_w])
        args_list.extend(other_flags)
        args_list.append('-target*')
        # Append applyChoiceChangesXML if present
        if xml_path_w:
            args_list.extend(['-applyChoiceChangesXML', xml_path_w])
        # Build a second variant that explicitly specifies '-target /' and
        # allows any trailing arguments via a final '*'.  This improves
        # compatibility with installers that pass '-target /' without
        # additional flags.  We do not duplicate xml flags on this
        # variant because they were already included in args_list above.
        args_list2: List[str] = []
        args_list2.extend(['-pkg', pkg_path_w])
        args_list2.extend(other_flags)
        args_list2.extend(['-target', '/'])
        if xml_path_w:
            args_list2.extend(['-applyChoiceChangesXML', xml_path_w])
        # Compose the two rule lines.  The first uses '-target*' and
        # join_command without trailing star; the second uses explicit
        # '-target /' with allow_trailing_star=True.
        line1 = f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(cmd, args_list)
        line2 = f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(cmd, args_list2, allow_trailing_star=True)
        return line1 + '\n' + line2
    # Normalise arbitrary scripts under Caskroom with version directories
    if cmd.startswith(brew_prefix) and '/Caskroom/' in cmd:
        parts = cmd.split('/')
        try:
            idx = parts.index('Caskroom')
            if idx + 2 < len(parts):
                parts[idx + 2] = '*'
            cmd = '/'.join(parts)
        except ValueError:
            pass
        return f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(cmd, args, allow_trailing_star=False)
    # Normalise launchctl list/remove from logs.  Handle both /bin/launchctl
    # and /usr/bin/launchctl variants (basename == 'launchctl').
    if os.path.basename(cmd) == 'launchctl' and args:
        action = args[0]
        label = args[1] if len(args) > 1 else ''
        # Use wildcard helpers to unify label
        wildcarded = []
        if '.helper' in label:
            base = label.rsplit('.', 1)[0]
            wildcarded.append(base + '.*')
        wildcarded.append(label)
        lines = []
        for lbl in wildcarded:
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: /bin/launchctl {action} {lbl}")
            # Remove plist for list action
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: /bin/rm -f -- /Library/LaunchDaemons/{lbl}.plist")
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: /bin/rm -f -- /Library/LaunchAgents/{lbl}.plist")
        return '\n'.join(lines)
    # Fallback: wildcard deletion path for other commands
    # unify arguments if they look like paths
    norm_args = []
    for a in args:
        if a.startswith('/'):
            norm_args.append(_wildcard_delete_path(a))
        else:
            norm_args.append(a)
    return f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(cmd, norm_args)


def process_log_file(log_path: str, user: str, brew_prefix: str) -> List[str]:
    """Return sudoers rule lines parsed from a brew install log.

    Iterates over lines containing `/usr/bin/sudo` and generates rules
    for each unique command.  Filters out descriptive messages and
    ignores lines with Ruby object dumps.  Applies wildcarding via
    `_normalize_log_command`.
    """
    rules = []
    seen = set()
    try:
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as fh:
            for raw in fh:
                if '/usr/bin/sudo' not in raw:
                    continue
                # Skip descriptive messages
                if 'with `sudo`' in raw or 'Uninstalling packages' in raw or 'Changing ownership' in raw:
                    continue
                if 'Running installer' in raw:
                    continue
                # Skip Ruby object dumps and other Ruby inspector outputs
                if '#<Cask' in raw or 'Cask::' in raw or '@dsl_args' in raw or '@directives' in raw or '@cask=' in raw:
                    continue
                # Skip sudo error/warning lines (e.g. "sudo: 3 incorrect password attempts")
                stripped = raw.strip()
                if stripped.startswith('sudo:'):
                    continue
                # Tokenise
                try:
                    tokens = shlex.split(raw, posix=True)
                except Exception:
                    continue
                parsed = _find_log_command_tokens(tokens)
                if not parsed:
                    continue
                cmd, args = parsed
                # Generate rule(s)
                rule = _normalize_log_command(cmd, args, user, brew_prefix)
                if not rule:
                    continue
                for r in rule.split('\n'):
                    # Apply version wildcarding on the entire rule.  Without this
                    # post‑processing, rules derived from log lines may embed
                    # explicit version numbers (e.g. "5.30", "v2", "arm64-apple-macosx26")
                    # which would require password prompts again on upgrades.  By
                    # wildcarding here we unify those numeric segments across the
                    # whole rule string.  This call is idempotent for strings
                    # already wildcarded by earlier helpers.
                    r = _wildcard_versions_in_rule(r)
                    if r not in seen:
                        rules.append(r)
                        seen.add(r)
    except Exception:
        # If log file missing or unreadable, return empty
        return []
    return rules


def determine_dest_dir(uninst: Dict[str, List[Any]]) -> Optional[str]:
    """Infer the destination directory for app bundles from uninstall paths.

    Some casks specify `$APPDIR` or `#{appdir}` in their uninstall `trash`
    or `delete` directives.  Replace these with `/Applications/<dir>`.
    """
    pattern = re.compile(r"^(?:\$APPDIR|#\{appdir\})/(?P<dir>[^/]+)")
    for key in ("trash", "delete"):
        for path in uninst.get(key, []):
            m = pattern.match(str(path))
            if m:
                return os.path.join("/Applications", m.group("dir"))
    return None


###############################################################################
# Wildcard helper functions

TEAM_ID_RE = re.compile(r"^[A-Z0-9]{8,12}$")


def _wildcard_team_id(segment: str) -> str:
    """Replace developer team ID segments with a wildcard.

    A team ID is 8–12 uppercase alphanumerics.  If a path segment is
    exactly a team ID, return '*' instead.  Handles segments starting
    with '*' by leaving the star intact (e.g. '*RR9LPM2N' -> '*').
    """
    prefix = ''
    while segment.startswith('*'):
        prefix += '*'
        segment = segment[1:]
    return prefix + ('*' if TEAM_ID_RE.match(segment) else segment)


def _wildcard_delete_path(path: str) -> str:
    """Wildcard variable portions in deletion paths.

    * Replace team IDs with '*'.
    * Replace numeric or version‑like segments with '*':
        - purely numeric segments (e.g. '2025basic' -> '*basic')
        - segments where digits/dots/commas predominate (e.g. '13.6.3,24585314' -> '*')
        - digits preceded by space/hyphen/underscore (e.g. 'Folx3' -> 'Folx*').
        - trailing digits preceded by letters (e.g. 'macgpg2' -> 'macgpg*').
        - 'v' version prefixes (e.g. 'v1.3.7' -> 'v*').
    Collapses multiple '*' in a segment to a single '*'.
    """
    parts = []
    for seg in path.split('/'):
        if not seg or seg in ('~', '..', '.'):
            parts.append(seg)
            continue
        orig = seg
        # Handle team IDs
        seg = _wildcard_team_id(seg)
        # If the segment is reduced to a single '*' (team ID), skip further
        # transformations to avoid reintroducing partial wildcards.
        if seg == '*':
            parts.append(seg)
            continue
        # Only wildcard segments that consist entirely of digits and common version separators
        # such as dots, commas, underscores or hyphens.  Do not wildcard segments that
        # merely start with a digit but also contain letters (e.g. '115Browser.app').  This
        # avoids dangerous patterns like '/Applications/*.app' while still wildcarding
        # purely numeric or version‑like directory names (e.g. '13.6.3,24585314_0521' -> '*').
        if re.fullmatch(r'[\d.,_-]+', seg):
            seg = '*'
        # Replace segments like '13.6.3,24585314_0521' entirely (orig may include
        # characters stripped by team ID processing above).
        if re.fullmatch(r'[\d.,_-]+', orig):
            seg = '*'
        # Replace digits preceded by space/hyphen/underscore
        seg = re.sub(r'(?:\s|-|_)(?:\d+)(?:[\d.,]*)', lambda m: m.group(0)[0] + '*', seg)
        # Replace trailing digits after letters (e.g. 'macgpg2' -> 'macgpg*')
        seg = re.sub(r'([A-Za-z])\d+(?:[\d.,]*)$', r'\1*', seg)

        # Replace digits preceding an uppercase letter within the segment.  This
        # helps generalise names like 'Folx3Plugin' -> 'Folx*Plugin'.  We look
        # for digits following a letter and immediately before an uppercase
        # letter, and replace the digit sequence with '*'.
        seg = re.sub(r'([A-Za-z])\d+(?=[A-Z])', r'\1*', seg)

        # If segment contains an extension (e.g. 'com.techsmith.camtasia25.sfl'),
        # wildcard digits immediately before the final dot.  This allows
        # version numbers embedded in filenames to be generalised.  We treat
        # only the last component before the extension to avoid affecting
        # domain prefixes.
        if '.' in seg and not re.fullmatch(r'[\d.,]+', seg):
            # Split on the last dot into name and extension
            base, dot, ext = seg.rpartition('.')
            if base:
                # If base ends with digits preceded by a letter, wildcard them
                new_base = re.sub(r'([A-Za-z])\d+(?:[\d.,]*)$', r'\1*', base)
                if new_base != base:
                    seg = new_base + dot + ext
        # Replace v‑prefixed versions
        seg = re.sub(r'v\d+(?:[\d.]+)*', 'v*', seg)
        # Collapse multiple stars within the segment
        seg = re.sub(r'\*+', '*', seg)
        # Collapse team identifier segments containing stars.  Two cases:
        # (1) If the segment consists solely of uppercase letters/digits with
        #     embedded stars and no other punctuation (e.g. '7SFX*GNR7'),
        #     collapse the entire segment to '*'.  This prevents internal
        #     star positions from leaking.
        # (2) If the segment contains a star‑separated prefix followed by a
        #     dot and suffix (e.g. '86Z*GCJ*MF.com.noodlesoft.HazelHelper.plist'),
        #     examine only the prefix before the dot.  If removing stars
        #     yields a string of ≥6 uppercase letters/digits, collapse
        #     just that prefix to '*', preserving the dot and suffix
        #     (yielding '*.com.noodlesoft.HazelHelper.plist').
        if '*' in seg:
            # Case 2: collapse star‑separated prefix before first dot
            if '.' in seg:
                pre, dot, suf = seg.partition('.')
                clean_pre = pre.replace('*', '')
                if len(clean_pre) >= 6 and re.fullmatch(r'[A-Z0-9]+', clean_pre) and re.fullmatch(r'[A-Z0-9\*]+', pre):
                    seg = '*' + dot + suf
                else:
                    # Fall back to case 1 on entire segment
                    clean = orig.replace('*', '')
                    if len(clean) >= 6 and re.fullmatch(r'[A-Z0-9]+', clean) and re.fullmatch(r'[A-Z0-9\*]+', seg):
                        seg = '*'
            else:
                clean = orig.replace('*', '')
                if len(clean) >= 6 and re.fullmatch(r'[A-Z0-9]+', clean) and re.fullmatch(r'[A-Z0-9\*]+', seg):
                    seg = '*'
        parts.append(seg)
    return '/'.join(parts)


def _wildcard_app_path(path: str) -> str:
    """Wildcard version numbers in `.app` paths.

    For app bundle names containing version numbers after spaces,
    hyphens, underscores or 'v' prefixes, replace the numeric part
    with '*'.  Also wildcard any team IDs or numeric directory names.
    """
    # Split path to apply wildcards to each segment
    segments = path.split('/')
    new_segments = []
    for seg in segments:
        if seg.endswith('.app'):
            base = seg[:-4]
            # e.g. 'SMS Plus v1.3.7' -> 'SMS Plus v*'
            base = re.sub(r'(?:\s|\-|_)(?:v?\d+(?:[\d.]+)*)$', lambda m: m.group(0)[0] + '*', base)
            # e.g. 'Folx3' -> 'Folx*'
            base = re.sub(r'(.*?)(\d+(?:[\d.]+)*)$', r'\1*', base)
            seg = base + '.app'
        seg = _wildcard_delete_path(seg)
        new_segments.append(seg)
    return '/'.join(new_segments)


def _wildcard_pkg_name(pkg: str) -> str:
    """Wildcard version numbers in package (.pkg) filenames.

    Replaces sequences of digits (with optional dots or underscores)
    following a hyphen or underscore with '*', e.g.
    'mactex-basictex-20250308.pkg' -> 'mactex-basictex-*.pkg'.  Also
    collapses multiple '*-'.
    """
    # Replace parenthesised numeric sequences, e.g. '(5558)', with '*'
    name = re.sub(r'\(\d+\)', '*', pkg)
    name = re.sub(r'([_-])\d[\d._,]*', r'\1*', name)
    # Collapse repeated -* or _*
    name = re.sub(r'(?:-_*\*)+', '-*', name)
    name = re.sub(r'(?:__\*)+', '_*', name)
    # Collapse consecutive stars into a single star
    name = re.sub(r'\*+', '*', name)
    return name


def _wildcard_script_name(script: str) -> str:
    """Wildcard numeric segments in script executables.

    For installer or uninstall script names containing version or date
    sequences separated by hyphens or underscores, replace the numeric
    portion with '*'.  For example, 'Anaconda3-2025.06-1-MacOSX-arm64.sh'
    becomes 'Anaconda*-*-MacOSX-arm64.sh'.  Collapses repeated '-*'
    segments.
    """
    s = re.sub(r'([_-])\d[\d._]*', r'\1*', script)
    # Replace sequences of v + digits
    s = re.sub(r'v\d[\d.]*', 'v*', s)
    # Collapse repeated -* or _*
    s = re.sub(r'(?:-_*\*)+', '-*', s)
    s = re.sub(r'(?:__\*)+', '_*', s)
    # Collapse consecutive stars to a single star
    s = re.sub(r'\*+', '*', s)
    return s


def _wildcard_pkgutil_id(pid: str) -> str:
    """Wildcard trailing numeric version in pkgutil IDs.

    If the last component of the ID ends with a sequence of digits,
    replace the digits with '*'.  e.g. 'org.tug.mactex.basictex2025'
    -> 'org.tug.mactex.basictex*'.
    """
    return re.sub(r'\d+$', '*', pid)

# -----------------------------------------------------------------------------
# Helper function to wildcard version identifiers in complete sudoers rules.
#
# In addition to wildcarding specific path or label components, some log
# entries still embed version numbers in paths, package names or labels that
# were not captured by earlier wildcard rules.  If left intact, these
# version numbers can cause sudoers rules to break when the underlying
# application is updated.  The `_wildcard_versions_in_rule` helper
# performs conservative text substitutions on an entire rule to replace
# version‑like tokens with wildcards.

def _wildcard_versions_in_rule(rule: str) -> str:
    """Replace version identifiers in a sudoers rule with wildcards.

    Parameters
    ----------
    rule : str
        A single sudoers rule line of the form ``user ALL=(ALL) NOPASSWD: SETENV: …``.

    Returns
    -------
    str
        The rule with variable version components replaced by '*'.
    """
    # Replace version directories in Caskroom paths (e.g.
    # /Caskroom/foo/1.2.3 -> /Caskroom/foo/*).  Only the immediate
    # subdirectory after <cask> is wildcarded.
    rule = re.sub(r'(/Caskroom/[^/]+/)[^/]+', r'\1*', rule)
    # Replace sequences of numbers separated by punctuation characters such
    # as ., -, _, , or parentheses with a single '*'.  We require at
    # least one separator to avoid matching simple integers.  Examples:
    # 1.2.3, 6.0.4-1234, 2_5_0, 3_7_1(5558) -> *.
    rule = re.sub(r'\b\d+[\.\-_,()]\d+(?:[\.\-_,()]\d+)*\b', '*', rule)
    # Replace version numbers that appear after a dot without a preceding
    # letter (e.g. '.2025', '.0.20.1').  This helps wildcard API
    # identifiers or bundle names like 'LayOut.2025.LayOutThumbnailExtension'.
    rule = re.sub(r'\.\d+(?:[\.\-_,]\d+)*', '.*', rule)
    # Replace standalone eight‑digit sequences (often dates) with '*'
    rule = re.sub(r'\b\d{8}\b', '*', rule)
    # Replace four‑digit sequences when preceded by a letter, dot, underscore
    # or hyphen.  This catches year‑like segments such as '.2025' or '_2024'.
    rule = re.sub(r'(?<=[A-Za-z._-])\d{4}\b', '*', rule)
    # Replace macOS SDK suffixes like 'macosx26' or 'macos10' with a wildcard.
    rule = re.sub(r'macosx\d+', 'macosx*', rule)
    rule = re.sub(r'macos\d+', 'macos*', rule)
    # Replace architecture targets like 'arm64' with 'arm*' to allow
    # future CPU variations (e.g. arm65).  Only collapse the numeric
    # portion after 'arm'.
    rule = re.sub(r'arm\d+', 'arm*', rule)
    # Replace occurrences of 'v' followed by digits (e.g. v2, v10) with 'v*'.
    rule = re.sub(r'\bv\d+\b', 'v*', rule)
    # Replace hyphen‑prefixed dotted version numbers like '-1.0' or '-3.4.5'
    # with '-*'.  This captures minor or patch versions embedded in
    # launchctl labels and other identifiers (e.g. 'com.adobe.AAM.Startup-1.0').
    rule = re.sub(r'-(?:\d+\.)+\d+', '-*', rule)
    # Replace single‑digit ordinals (e.g. '3rd', '1st', '2nd', '4th') with '*'.
    rule = re.sub(r'\d+(?:st|nd|rd|th)', '*', rule)
    # Replace digits preceded by a letter and followed by a dot or hyphen.  This
    # handles cases like 'net9.Welly' -> 'net*.Welly' and 'foo3-bar' -> 'foo*-bar'.
    rule = re.sub(r'(?<=[A-Za-z])\d+(?=[\.\-])', '*', rule)
    # Replace digits preceded by a letter and followed by another letter.  This
    # handles CamelCase or concatenated identifiers such as 'numi3helper' and
    # 'BlueHarvestHelper8' by wildcarding the numeric component.  It will
    # transform them to 'numi*helper' and 'BlueHarvestHelper*'.
    rule = re.sub(r'([A-Za-z])\d+(?=[A-Za-z])', r'\1*', rule)
    # Replace digits preceded by a letter at the end of a word (not followed
    # by another alphanumeric character).  This covers patterns like
    # 'BlueHarvestHelper8' -> 'BlueHarvestHelper*' and 'numi3' -> 'numi*'.
    rule = re.sub(r'([A-Za-z])\d+(?=[^A-Za-z0-9]|$)', r'\1*', rule)
    # Replace long hexadecimal or alphanumeric tokens (8+ characters) that
    # resemble commit hashes or unique identifiers with '*'.
    rule = re.sub(r'\b[0-9a-fA-F]{8,}\b', '*', rule)
    # Replace hyphen‑prefixed alphanumeric fragments of 5 or more characters
    # that include at least one digit (e.g. '-5b3ous', '-cc24aef4') with
    # '-*'.  This helps wildcard variable suffixes in filenames like
    # 'choices20250918-92814-5b3ous.xml' without matching normal
    # hyphenated words such as '-teams'.
    rule = re.sub(r'-(?=[0-9A-Za-z]*\d)[0-9A-Za-z]{5,}', '-*', rule)
    # Collapse repeated '-*' patterns into a single '-*'
    rule = re.sub(r'(?:-\*){2,}', '-*', rule)
    # Collapse consecutive stars into a single star
    rule = re.sub(r'\*+', '*', rule)
    # Replace numeric sequences separated by dots or underscores that are
    # embedded within alphanumeric tokens.  For example, convert
    # 'iMazing3.4.0.23220Mac' -> 'iMazing*Mac' and
    # 'OpenVPN_Connect_3_7_1' -> 'OpenVPN_Connect_*'.  We look for a
    # digit sequence followed by one or more occurrences of a separator
    # (dot or underscore) plus additional digits, and require that it be
    # immediately preceded by a letter.  This avoids matching IP
    # addresses or plain numeric segments already handled above.
    rule = re.sub(r'(?<=[A-Za-z])\d+(?:[._]\d+)+', '*', rule)
    # Replace parenthesised numeric or version sequences like '(5558)' or
    # '(1.2.3)' with a single '*'.  This helps generalise installer
    # package names that embed build numbers.
    rule = re.sub(r'\(\d+(?:[\d._]*?)\)', '*', rule)
    # Collapse ARMDCHelper cc suffixes: if a label contains '.cc' followed
    # by a long sequence of hex digits (optionally interspersed with
    # previously inserted stars), replace the entire suffix after '.cc'
    # with a single '*'.  This handles log entries where the hash has
    # already been partially wildcarded (e.g. 'cc*aef*a*b*ed...').
    rule = re.sub(r'(\.cc)(?:[0-9A-Fa-f\*]{8,})', r'\1*', rule)
    # Collapse multiple '-*' or '_*' patterns that may have been
    # introduced by the substitutions above.  Ensure we don't end up with
    # '-*-*' or similar.
    rule = re.sub(r'(?:-\*){2,}', '-*', rule)
    rule = re.sub(r'(?:_\*){2,}', '_*', rule)
    # Collapse remaining consecutive stars once more
    rule = re.sub(r'\*+', '*', rule)
    return rule


def _wildcard_launchctl_labels(label: str) -> List[str]:
    """Generate wildcarded variants of a launchctl label.

    This helper produces additional patterns to match launchctl labels
    that may vary across versions or runtime identifiers.  Patterns
    handled include:

    * Hash‑like suffixes consisting of a dot followed by 8+ hex digits.
    * Version numbers or digits following a hyphen or dot, optionally
      prefaced by a `v` (e.g. ``.v2`` or ``-1.0``).
    * Trailing digits on the final component (e.g. ``gifox2.agent``).

    In addition, for labels beginning with ``com.`` we emit a second
    variant of the form ``application.<label>.installer*`` to cover
    temporary launchd jobs spawned by manual installers.

    Args:
        label: The original launchctl label from the cask.

    Returns:
        A list of wildcarded label strings.  May be empty.
    """
    variants: List[str] = []
    # Special handling: if the label contains a '.helper' component (e.g.
    # 'com.adguard.mac.adguard.helper'), generate a variant that wildcards
    # the helper suffix.  This helps match both 'helper' and possible
    # future helper variants (e.g. 'helper2', 'helperService').
    if '.helper' in label:
        helper_base = label.split('.helper', 1)[0]
        if helper_base:
            variants.append(f"{helper_base}.*")

    wild = None
    # Hash‑like suffix
    if re.search(r'\.[a-f0-9]{8,}$', label):
        wild = re.sub(r'\.[a-f0-9]{8,}$', '.*', label)
    # Hyphen or dot followed by version digits
    elif re.search(r'[\.-]v?\d(?:[\d.]+)*$', label):
        wild = re.sub(r'([\.-])v?\d(?:[\d.]+)*$', r'\1*', label)
    # Embedded digit at end of component
    elif re.search(r'[A-Za-z]\d+$', label):
        wild = re.sub(r'([A-Za-z])\d+$', r'\1*', label)
    if wild and wild != label:
        variants.append(wild)
    # Always include an application installer variant; if a wildcard label was
    # generated, use it as the base for the application variant.  Otherwise
    # use the original label.  This ensures version digits or hashes are
    # wildcarded in the application label as well (e.g. '...v2' -> '...*').
    if label.startswith('com.'):
        base_for_app = wild if wild else label
        variants.append(f"application.{base_for_app}.installer*")
    return variants


def _wildcard_cask_path(path: str, token: str) -> str:
    """Wildcard the version portion of a Caskroom path.

    Given a path like
      /opt/homebrew/Caskroom/chatgpt/1.2025.245,1757119478/ChatGPT.app
    return a path that replaces the version directory with '*':
      /opt/homebrew/Caskroom/chatgpt/*/ChatGPT.app
    Also applies `_wildcard_delete_path` to the remaining segments.
    """
    prefix = os.path.join(run(["brew", "--prefix"]) , 'Caskroom', token)
    if path.startswith(prefix):
        suffix = path[len(prefix):].lstrip('/')
        parts = suffix.split('/')
        if parts:
            parts[0] = '*'
        suffix_w = '/'.join(parts)
        out = os.path.join(prefix, suffix_w)
    else:
        out = path
    return _wildcard_delete_path(out)


###############################################################################
# Core rule generation

def generate_sudoers_for_cask(token: str, cj: Dict[str, Any], user: str,
                              brew_prefix: str, swift_util: str) -> List[str]:
    """Return a list of sudoers lines for a single cask.

    Applies wildcarding to paths, names and identifiers to ensure the
    rules remain valid across version changes.  Handles detection of
    privileged operations including copying/removing apps, installing
    packages, executing scripts, managing launchctl services, forgetting
    pkgutil receipts, and deleting files and directories.
    """
    arts = parse_artifacts(cj)
    # Extract relevant data
    apps: List[Tuple[str, Optional[str]]] = []
    pkgs: List[str] = []
    installer_scripts: List[Dict[str, Any]] = []
    installer_manuals: List[str] = []
    uninst_pkgutil: List[str] = []
    uninst_launch: List[str] = []
    uninst_scripts: List[Dict[str, Any]] = []
    uninst_delete: List[str] = []
    uninst_rmdir: List[str] = []
    uninst_setown: List[str] = []
    uninst_trash: List[str] = []
    uninst_kexts: List[str] = []
    uninst_signals: List[Tuple[str, str]] = []
    # Processes listed under uninstall->quit directives (to be terminated via killall)
    uninst_quit: List[str] = []
    for obj in arts:
        if 'app' in obj:
            for entry in ensure_list(obj['app']):
                if isinstance(entry, str):
                    apps.append((entry, None))
                elif isinstance(entry, dict):
                    src = entry.get('path') or entry.get('source') or entry.get('app') or entry.get('target') or ''
                    tgt = entry.get('target')
                    if not src and tgt:
                        src = os.path.basename(tgt)
                    if src:
                        apps.append((src, tgt))
        if 'pkg' in obj:
            for entry in ensure_list(obj['pkg']):
                if isinstance(entry, str):
                    pkgs.append(entry)
        if 'installer' in obj:
            inst = obj['installer']
            if isinstance(inst, dict):
                if inst.get('script') and inst['script'].get('sudo'):
                    installer_scripts.append({'exec': inst['script']['executable'], 'args': ensure_list(inst['script'].get('args'))})
                if inst.get('manual'):
                    installer_manuals.append(inst['manual'])
            elif isinstance(inst, list):
                for it in inst:
                    if not isinstance(it, dict):
                        continue
                    if it.get('script') and it['script'].get('sudo'):
                        installer_scripts.append({'exec': it['script']['executable'], 'args': ensure_list(it['script'].get('args'))})
                    if it.get('manual'):
                        installer_manuals.append(it['manual'])
        if 'uninstall' in obj:
            for entry in ensure_list(obj['uninstall']):
                if not isinstance(entry, dict):
                    continue
                uninst_pkgutil += ensure_list(entry.get('pkgutil'))
                uninst_launch += ensure_list(entry.get('launchctl'))
                uninst_delete += ensure_list(entry.get('delete'))
                uninst_rmdir += ensure_list(entry.get('rmdir'))
                uninst_trash += ensure_list(entry.get('trash'))
                # collect kernel extensions (kext) identifiers for unloading
                uninst_kexts += ensure_list(entry.get('kext'))
                # Collect uninstall scripts.  Handle both `script` and
                # `early_script` keys.  Some casks specify an "early_script"
                # (or uninstall_preflight) that should run with sudo.  Treat
                # these similarly to regular uninstall scripts.
                script_entry = entry.get('script')
                early_script_entry = entry.get('early_script')
                for scr in (script_entry, early_script_entry):
                    if scr and isinstance(scr, dict) and scr.get('sudo'):
                        uninst_scripts.append({
                            'exec': scr.get('executable'),
                            'args': ensure_list(scr.get('args'))
                        })

                # Some uninstall definitions use "uninstall_preflight" or
                # "uninstall_postflight" keys with procs (e.g. Adobe).  These
                # callbacks may run privileged commands (like pluginkit
                # invocations) but they are executed internally by brew and
                # cannot be whitelisted generically.  We ignore them here.

                if entry.get('set_ownership'):
                    uninst_setown += ensure_list(entry['set_ownership'])
                # collect signal directives for pkill/killall operations
                sigs = entry.get('signal')
                if sigs:
                    for sig in ensure_list(sigs):
                        # Expect [SIGNAL, PROCESS] pairs
                        if isinstance(sig, list) and len(sig) == 2:
                            signame, process = sig
                            uninst_signals.append((str(signame), str(process)))

                # collect processes to quit gracefully via killall
                qu = entry.get('quit')
                if qu:
                    for proc in ensure_list(qu):
                        if isinstance(proc, str):
                            uninst_quit.append(proc)
        if 'zap' in obj:
            for entry in ensure_list(obj['zap']):
                if isinstance(entry, dict):
                    uninst_delete += ensure_list(entry.get('delete'))
                    uninst_rmdir += ensure_list(entry.get('rmdir'))
                    uninst_trash += ensure_list(entry.get('trash'))

    # Determine destination override from uninstall entries
    dest_override = determine_dest_dir({'delete': uninst_delete, 'trash': uninst_trash})
    lines: List[str] = []
    # Generate rules for each app
    for src_rel, tgt_rel in apps:
        # Source glob and destination path
        src_glob = os.path.join(brew_prefix, 'Caskroom', token, '*', src_rel)
        src_glob_w = _wildcard_cask_path(src_glob, token)
        # Further generalise the source app path itself (e.g. 'Alfred 5.app'
        # -> 'Alfred *.app') by wildcarding numeric suffixes in the final
        # bundle name.  This uses `_wildcard_app_path` which handles
        # version numbers after spaces, hyphens or underscores.  Without
        # this additional step, cp rules for apps like Alfred 5 will
        # remain version‑specific in the source path.
        src_glob_w = _wildcard_app_path(src_glob_w)
        if tgt_rel:
            dest_path = os.path.join('/Applications', tgt_rel)
        else:
            dest_dir = dest_override or '/Applications'
            # If override points to an app bundle path, use that; else join src name
            if dest_override and dest_override.lower().endswith('.app'):
                dest_path = dest_override
            else:
                dest_path = os.path.join(dest_dir, src_rel)
        dest_path_w = _wildcard_app_path(dest_path)
        # 1. Remove app bundle and its contents
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-R', '-f', '--', dest_path_w]))
        # Remove contents directory separately (brew may call this)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-R', '-f', '--', os.path.join(dest_path_w, 'Contents')]))
        # 2. Remove sentinel file if present (both variants)
        sentinel = os.path.join(dest_path_w, '.homebrew-write-test')
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/usr/bin/touch', [sentinel.replace(' ', '\\ ')]) )
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', [sentinel.replace(' ', '\\ ')]) )
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-f', '--', sentinel.replace(' ', '\\ ')]) )
        # 3. Copy app and contents
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/cp', ['-pR', src_glob_w, dest_path_w]))
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/cp', ['-pR', os.path.join(src_glob_w, 'Contents'), dest_path_w]))
        # 4. Copy extended attributes (system swift and CLT swift)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/usr/bin/swift', ['-target', 'arm64-apple-macosx*', swift_util, src_glob_w, dest_path_w]))
        clt_swift = os.path.join('/Library/Developer/CommandLineTools/usr/bin/swift')
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(clt_swift, ['-target', 'arm64-apple-macosx*', swift_util, src_glob_w, dest_path_w]))

        # 5. Fix ownership of the installed application bundle.  Many
        #    installers call chown to set the owner and group on the
        #    destination path.  We allow this without a password.
        #    Use a wildcard for the user portion to match any username,
        #    as cask uninstall scripts may specify different users.
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/usr/sbin/chown', ['-R', '--', '*:staff', dest_path_w]))

    # pkg installers
    for pkg in pkgs:
        pkg_w = _wildcard_cask_path(os.path.join(brew_prefix, 'Caskroom', token, '*', _wildcard_pkg_name(pkg)), token)
        # Permit installer invocation with a wildcard target to accommodate
        # variations like '-target / -verboseR' or other flags.  We use
        # '*'' after '-target' rather than a literal '/' to allow optional
        # arguments following the target.  Do not append a trailing '*' to
        # avoid producing overly broad rules.
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/usr/sbin/installer', ['-pkg', pkg_w, '-target', '*']))

    # installer scripts
    for sc in installer_scripts:
        exe = sc['exec']
        if not exe.startswith('/'):
            exe = os.path.join(brew_prefix, 'Caskroom', token, '*', exe)
        exe_w = _wildcard_cask_path(_wildcard_script_name(exe), token)
        args = sc.get('args', [])
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(exe_w, args, allow_trailing_star=True))

    # manual installers (app within DMG)
    for man in installer_manuals:
        # assume manual is an app bundle; wildcard version path
        exe_glob = os.path.join(brew_prefix, 'Caskroom', token, '*', man, 'Contents', 'MacOS', os.path.splitext(os.path.basename(man))[0] + '*')
        exe_w = _wildcard_cask_path(exe_glob, token)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(exe_w, [], allow_trailing_star=True))

    # pkgutil forget
    for pid in uninst_pkgutil:
        pid_w = _wildcard_pkgutil_id(str(pid))
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/usr/sbin/pkgutil', ['--forget', pid_w]))

    # launchctl operations
    for lbl in uninst_launch:
        lbl_str = str(lbl)
        # Generate wildcarded variants for the label.  If wildcard variants exist,
        # prefer them over the original label to avoid enumerating both the
        # versioned and wildcard patterns (e.g. 'BlueHarvestHelper8' vs
        # 'BlueHarvestHelper*').  If no wildcard variant is returned, use the
        # original label.
        # Generate a list of labels to use for this launchctl label.  Always
        # include the original label, and then append any wildcard variants
        # returned by _wildcard_launchctl_labels().  This ensures that
        # launchctl commands for the base label (e.g. com.adobe.agsservice)
        # are permitted, while also covering installer variants and helper
        # versions.  Deduplicate while preserving order.
        base_labels: List[str] = [lbl_str]
        variants = _wildcard_launchctl_labels(lbl_str)
        for v in variants:
            if v not in base_labels:
                base_labels.append(v)
        for label in base_labels:
            for action in ('list', 'remove'):
                lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/launchctl', [action, label]))
            # Remove associated launchd plist files for each label
            for base_dir in ('/Library/LaunchDaemons', '/Library/LaunchAgents'):
                plist_path = os.path.join(base_dir, label + '.plist')
                p_w = _wildcard_delete_path(plist_path)
                lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-f', '--', p_w]))
        # If the original label contains '.helper', also add removal of its exact plist.
        # This is redundant if the helper wildcard variant is identical to the
        # original label, but harmless.
        if '.helper' in lbl_str:
            for base_dir in ('/Library/LaunchDaemons', '/Library/LaunchAgents'):
                helper_plist = os.path.join(base_dir, lbl_str + '.plist')
                p_w = _wildcard_delete_path(helper_plist)
                lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-f', '--', p_w]))

    # uninstall scripts (sudo)
    for sc in uninst_scripts:
        exe = sc['exec']
        if not exe.startswith('/'):
            exe = os.path.join(brew_prefix, 'Caskroom', token, '*', exe)
        exe_w = _wildcard_cask_path(_wildcard_script_name(exe), token)
        args = sc.get('args', [])
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(exe_w, args, allow_trailing_star=True))

    # delete/trash paths
    # For each path slated for deletion or trash, generate both recursive
    # removal and single‑file removal rules.  Homebrew will sometimes
    # call `rm -rf` on directories and `rm -f` on individual files.
    for p in (uninst_delete + uninst_trash):
        # Only process string paths
        if not isinstance(p, str):
            continue
        p_w = _wildcard_delete_path(str(p))
        # Recursive removal (-r -f)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-r', '-f', '--', p_w]))
        # Non‑recursive removal (-f)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-f', '--', p_w]))

    # rmdir paths
    # Expand brace patterns in rmdir paths (e.g. Adobe{/CEP{/extensions,},}) into
    # individual directories.  Sudoers cannot interpret brace syntax, so
    # transform such patterns into explicit paths.  This implementation
    # recursively expands nested braces as well as top‑level comma lists.
    def expand_braces(path: str) -> List[str]:
        """Recursively expand brace expressions like foo{bar,baz}.

        Supports nested braces.  For example, the pattern
          '/Library/Application Support/Adobe{/CEP{/extensions,},}'
        expands to the three paths:
          '/Library/Application Support/Adobe/CEP/extensions'
          '/Library/Application Support/Adobe/CEP'
          '/Library/Application Support/Adobe'
        """
        # Find the first '{'
        l = path.find('{')
        if l == -1:
            return [path]
        # Find matching '}' for the first '{'
        depth = 0
        r = None
        for idx, ch in enumerate(path[l:], start=l):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    r = idx
                    break
        # If no closing brace found, return path as is
        if r is None:
            return [path]
        before = path[:l]
        inside = path[l+1:r]
        after = path[r+1:]
        # Split the inside on top‑level commas
        options: List[str] = []
        depth2 = 0
        start = 0
        for i, ch in enumerate(inside):
            if ch == '{':
                depth2 += 1
            elif ch == '}':
                depth2 -= 1
            elif ch == ',' and depth2 == 0:
                options.append(inside[start:i])
                start = i + 1
        options.append(inside[start:])
        results: List[str] = []
        # Recursively expand each option and the remainder of the string
        for opt in options:
            for opt_exp in expand_braces(opt):
                for after_exp in expand_braces(after):
                    results.append(before + opt_exp + after_exp)
        return results

    for p in uninst_rmdir:
        # Only process string paths
        if not isinstance(p, str):
            continue
        for exp in expand_braces(str(p)):
            p_w = _wildcard_delete_path(exp)
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rmdir', ['--', p_w]))

    # set ownership
    for p in uninst_setown:
        # Only process string paths
        if not isinstance(p, str):
            continue
        p_w = _wildcard_delete_path(str(p))
        # Use wildcard for user portion to match any username
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/usr/sbin/chown', ['-R', '--', '*:staff', p_w]))

    # kernel extension operations
    # For any kext identifiers specified in uninstall directives, permit
    # listing, loading and unloading the kext without a password.  To
    # avoid enumerating multiple possible locations, we dynamically
    # choose the path of kext utilities based on which exists on the
    # system.  On modern macOS, these utilities are typically in
    # /usr/sbin; older systems may have them in /sbin.  We pick the
    # first existing path.
    def choose_kext_path(cmd: str) -> str:
        for base in ('/usr/sbin', '/sbin'):
            p = os.path.join(base, cmd)
            if os.path.exists(p):
                return p
        # fallback to /usr/sbin
        return os.path.join('/usr/sbin', cmd)
    for kext in uninst_kexts:
        k = str(kext)
        # list loaded kernel extension
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(choose_kext_path('kextstat'), ['-l', '-b', k]))
        # unload the kernel extension
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(choose_kext_path('kextunload'), ['-b', k]))
        # allow loading as well (some installers load kexts)
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(choose_kext_path('kextload'), ['-b', k]))
        # permit searching for kexts
        lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(choose_kext_path('kextfind'), ['-b', k]))

    # signal operations (pkill/killall) for uninstall 'signal' directives
    # For each specified signal and process name, permit sending the signal
    # via pkill and killall.  We also generate variants of the process name
    # by applying _wildcard_launchctl_labels to cover version or hashed
    # suffixes.
    for signame, proc in uninst_signals:
        # Determine process variants: base and wildcarded
        proc_variants: List[str] = [proc]
        variants = _wildcard_launchctl_labels(proc)
        # If wildcard variants exist, prefer them over the exact process
        if variants:
            proc_variants = variants
        else:
            proc_variants = [proc]
        # Function to select pkill/killall path
        def choose_userbin(cmd: str) -> str:
            for base in ('/usr/bin', '/bin'):
                p = os.path.join(base, cmd)
                if os.path.exists(p):
                    return p
            return os.path.join('/usr/bin', cmd)
        # For each process variant, allow pkill and killall from chosen path
        for p in proc_variants:
            pkill_path = choose_userbin('pkill')
            killall_path = choose_userbin('killall')
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(pkill_path, [f"-{signame}", '-x', p]))
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(killall_path, [f"-{signame}", p]))

    # quit operations (killall without signal) for uninstall 'quit' directives
    # For each process listed in the cask's uninstall->quit, allow killall without a signal.
    # We also generate wildcard variants using _wildcard_launchctl_labels to cover hashed or versioned suffixes.
    for proc in uninst_quit:
        proc_variants: List[str] = [proc]
        variants = _wildcard_launchctl_labels(proc)
        # If wildcard variants exist, prefer them over the exact process
        if variants:
            proc_variants = variants
        else:
            proc_variants = [proc]
        # Choose killall path once
        def choose_userbin(cmd: str) -> str:
            for base in ('/usr/bin', '/bin'):
                p = os.path.join(base, cmd)
                if os.path.exists(p):
                    return p
            return os.path.join('/usr/bin', cmd)
        killall_path = choose_userbin('killall')
        for p in proc_variants:
            # Permit killall without a signal
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(killall_path, [p]))
            # Also allow removal of any associated launchd plist files for processes listed under quit.
            for base_dir in ('/Library/LaunchDaemons', '/Library/LaunchAgents'):
                plist_path = os.path.join(base_dir, p + '.plist')
                p_w = _wildcard_delete_path(plist_path)
                lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command('/bin/rm', ['-f', '--', p_w]))

    # ------------------------------------------------------------------
    # Additional cleanup operations used by Homebrew during pkg uninstall
    # Brew uses xargs to batch-remove files and directories when
    # forgetting pkgutil receipts.  These operations are executed via
    # sudo with xargs calling rm or the rmdir helper script.  To avoid
    # password prompts, we permit these patterns explicitly.  Note: we
    # do not wildcard the brew prefix here because brew_prefix is
    # passed into this function and resolves to the user's Homebrew
    # installation path.
    try:
        # Determine xargs binary path once (prefer /usr/bin, fallback to /bin)
        def choose_xargs() -> str:
            for base in ('/usr/bin', '/bin'):
                p = os.path.join(base, 'xargs')
                if os.path.exists(p):
                    return p
            return '/usr/bin/xargs'
        xargs_bin = choose_xargs()
        xargs_variants: List[Tuple[str, List[str]]] = [
            (xargs_bin, ['-0', '--', '/bin/rm', '--']),
            (xargs_bin, ['-0', '--', '/bin/rm', '-r', '-f', '--']),
            (xargs_bin, ['-0', '--', os.path.join(brew_prefix, 'Library', 'Homebrew', 'cask', 'utils', 'rmdir.sh')]),
        ]
        for exe, args in xargs_variants:
            lines.append(f"{user} ALL=(ALL) NOPASSWD: SETENV: " + join_command(exe, args))
    except Exception:
        # If brew_prefix is unavailable or os.path fails, skip adding xargs rules
        pass

    # Deduplicate lines
    unique_lines: List[str] = []
    seen = set()
    for ln in lines:
        if ln not in seen:
            seen.add(ln)
            unique_lines.append(ln)
    # Apply version wildcarding to all generated lines.  The log parser
    # already calls `_wildcard_versions_in_rule`, but rules originating
    # from cask metadata (artifacts, uninstall directives, etc.) need
    # version generalisation as well.  This ensures that year‑like
    # segments (e.g. '.2025.'), minor/patch versions ('-1.0') and
    # hashed suffixes are replaced with '*' consistently across the
    # entire sudoers file.
    for i, ln in enumerate(unique_lines):
        unique_lines[i] = _wildcard_versions_in_rule(ln)
    return unique_lines


def main() -> None:
    target_user = os.environ.get('TARGET_USER') or os.environ.get('SUDO_USER') or run(['id', '-un'])
    sudoers_out = os.environ.get('SUDOERS_OUT') or './homebrew-cask.nopasswd.sudoers'
    # Determine brew prefix and swift util path
    try:
        brew_prefix = run(['brew', '--prefix'])
        repo = run(['brew', '--repository'])
        swift_util = os.path.join(repo, 'Library', 'Homebrew', 'cask', 'utils', 'copy-xattrs.swift')
    except Exception:
        brew_prefix = '/opt/homebrew'
        swift_util = os.path.join(brew_prefix, 'Library', 'Homebrew', 'cask', 'utils', 'copy-xattrs.swift')
    # Determine casks to process
    casks_env = os.environ.get('CASKS')
    if casks_env:
        tokens = [c for c in casks_env.split() if c]
    else:
        try:
            out = run(['brew', 'list', '--cask'])
            tokens = [c for c in out.splitlines() if c]
        except Exception:
            tokens = []
    # Determine number of threads to use (default 32).  If THREADS is unset or invalid,
    # fallback to 32.  Ensure at least one thread.
    try:
        threads = int((os.environ.get('THREADS') or '').strip())
    except Exception:
        threads = 32
    if threads < 1:
        threads = 1

    # Worker to process a single cask
    def _process_cask(tok: str) -> Tuple[str, List[str]]:
        cj = fetch_cask_json(tok)
        if not cj:
            return tok, [f"# {tok}: failed to fetch metadata", ""]
        name_list = cj.get('name') or [tok]
        display = name_list[0]
        header = f"# ----------------------------\n# {display} ({tok})\n# ----------------------------"
        rules = generate_sudoers_for_cask(tok, cj, target_user, brew_prefix, swift_util)
        if not rules:
            return tok, [header, "# No privileged actions detected", ""]
        return tok, [header] + rules + [""]

    # Process casks, using a thread pool when beneficial
    results: List[Tuple[str, List[str]]] = []
    if threads > 1 and len(tokens) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=threads) as pool:
            futures = {pool.submit(_process_cask, tok): tok for tok in tokens}
            for fut in as_completed(futures):
                tok, lines = fut.result()
                results.append((tok, lines))
    else:
        # Fallback to sequential processing
        for tok in tokens:
            results.append(_process_cask(tok))

    # Preserve original order of tokens
    token_to_lines = {tok: lines for tok, lines in results}

    # Gather additional rules from logs (LOGS env or default .log file)
    extra_rules: List[str] = []
    used_logs: List[str] = []
    log_env = os.environ.get('LOGS')
    log_paths: List[str] = []
    if log_env:
        # One or multiple paths separated by ':'
        for lp in log_env.split(':'):
            lp = lp.strip()
            if lp:
                log_paths.append(lp)
    else:
        # Try a default log next to sudoers_out: same basename but .log
        if sudoers_out.endswith('.sudoers'):
            base = sudoers_out[:-len('.sudoers')]
        else:
            base = sudoers_out
        default_log = base + '.log'
        if os.path.exists(default_log):
            log_paths.append(default_log)
    # Process log files
    for log_path in log_paths:
        if os.path.exists(log_path):
            extra_rules.extend(process_log_file(log_path, target_user, brew_prefix))
            used_logs.append(log_path)
    # Warn if no logs were found
    if not log_paths:
        # Print a suggestion to specify LOGS or generate a log via reinstall_casks
        sys.stderr.write("[WARN] No log file specified or found. It is strongly recommended to specify a log via LOGS=path or place a .log file next to the sudoers output so that additional sudo commands can be captured. You can generate a log using reinstall_casks.py to reinstall your casks.\n")
    elif log_paths and not used_logs:
        sys.stderr.write(f"[WARN] Log file(s) specified but not found: {', '.join(log_paths)}\n")
    # Remove duplicates from extra_rules
    seen_extra = set()
    dedup_extra: List[str] = []
    for r in extra_rules:
        if r not in seen_extra:
            seen_extra.add(r)
            dedup_extra.append(r)

    # Write the sudoers file
    with open(sudoers_out, 'w', encoding='utf-8') as fh:
        fh.write("# ===== Homebrew Cask NOPASSWD rules (generated by gen_brew_cask_sudoers.py) =====\n")
        fh.write(f"# Generated: {run(['date','+%Y-%m-%d %H:%M:%S'])}\n")
        fh.write(f"# Target user: {target_user}\n\n")
        for tok in tokens:
            lines = token_to_lines.get(tok)
            if not lines:
                fh.write(f"# {tok}: failed to fetch metadata\n\n")
                continue
            for ln in lines:
                fh.write(ln + "\n")
        # Append extra rules from logs
        if dedup_extra:
            fh.write("\n# ----- Additional sudo commands from log -----\n")
            for ln in dedup_extra:
                fh.write(ln + "\n")

    print(f"✅ Generated sudoers snippet: {sudoers_out}")


if __name__ == '__main__':
    main()
