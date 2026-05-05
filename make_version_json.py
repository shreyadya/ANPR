"""
ANPR Update Manifest Generator
───────────────────────────────
Run this script from the workspace root AFTER doing a REBUILD.py build.
It scans the built files, computes SHA-256 hashes + sizes, and writes
version.json (for upload to GitHub) and updates local version.json.

Usage:
    python make_version_json.py

Then:
    1. Edit version.json — bump "version" and update "release_notes"
    2. git add version.json  updates/_internal_server.pyd  updates/.res3.enc  updates/.res4.enc ...
    3. git push
    Clients will auto-detect the update on next startup.
"""

import os
import sys
import json
import shutil
import hashlib
from datetime import date

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
# GitHub raw-file base URL for the published update repo.
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/shreyadya/ANPR/main/updates"

# Files to include in the update.
# "src"  : path inside ANPR_APP (source of the built file)
# "dest" : destination path inside the client's ANPR_APP folder
#
# ALWAYS included (core app files rebuilt every release):
UPDATE_FILES = [
    {"name": "_internal_server.pyd", "src": "ANPR_APP/_internal_server.pyd",  "dest": "_internal_server.pyd"},
    {"name": ".res2.enc",            "src": "ANPR_APP/.res2.enc",             "dest": ".res2.enc"},
    {"name": ".res3.enc",            "src": "ANPR_APP/.res3.enc",             "dest": ".res3.enc"},
    {"name": ".res4.enc",            "src": "ANPR_APP/.res4.enc",             "dest": ".res4.enc"},
    # {"name": "anpr.exe",             "src": "ANPR_APP/anpr.exe",            "dest": "anpr.exe"},
    {"name": "ANPR_SETUP.exe",       "src": "ANPR_APP/ANPR_SETUP.exe",        "dest": "ANPR_SETUP.exe"},
    {"name": "requirements.txt",     "src": "ANPR_APP/requirements.txt",      "dest": "requirements.txt"},
    # ── Launcher / updater (uncomment when you rebuild these exes) ──────────
    # {"name": "updater.exe",        "src": "ANPR_APP/updater.exe",           "dest": "updater.exe"},
    # ── Config / other files (uncomment as needed) ───────────────────────────
    # {"name": "config.json",        "src": "ANPR_APP/config.json",           "dest": "config.json"},
    # {"name": ".env",               "src": "ANPR_APP/.env",                  "dest": ".env"},
    # {"name": "wb_info.json",       "src": "ANPR_APP/wb_info.json",          "dest": "wb_info.json"},
]

# Output folder for update files (will be created if missing)
OUTPUT_DIR = "updates"

# ─────────────────────────────────────────────────────────────────────────────


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def main():
    # ── Load existing version.json so the user only needs to bump the version ──
    existing = {}
    previous_hashes = {}
    if os.path.exists("version.json"):
        with open("version.json", "r", encoding="utf-8") as f:
            existing = json.load(f)
            # Extract hashes from previous version for comparison
            for file_entry in existing.get("files", []):
                previous_hashes[file_entry["name"]] = file_entry["sha256"]

    version      = existing.get("version", "1.0.0")
    release_notes = existing.get("release_notes", "Bug fixes and improvements.")
    build_date   = str(date.today())

    print(f"\nANPR Update Manifest Generator (Incremental Updates)")
    print(f"Current version in version.json : {version}")
    new_version = input(f"New version to publish [{version}]: ").strip() or version
    new_notes   = input(f"Release notes [{release_notes}]: ").strip() or release_notes

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    file_entries = []
    changed_count = 0
    for item in UPDATE_FILES:
        src_path = item["src"].replace("/", os.sep)
        if not os.path.exists(src_path):
            print(f"  SKIP (not found): {src_path}")
            continue

        dst_name = item["name"]
        dst_path = os.path.join(OUTPUT_DIR, dst_name)

        print(f"  Hashing {src_path}…", end=" ", flush=True)
        digest = sha256_of(src_path)
        size   = os.path.getsize(src_path)
        
        # Check if file changed compared to previous version
        prev_hash = previous_hashes.get(dst_name)
        if prev_hash and prev_hash == digest:
            print(f"✓  {digest[:16]}…  (unchanged)")
            # Still add to manifest, but client won't re-download unchanged files
        else:
            status = "NEW" if not prev_hash else "CHANGED"
            print(f"✓  {digest[:16]}…  {size // 1024:,} KB  [{status}]")
            changed_count += 1
            # Copy to updates/ folder (only changed files)
            shutil.copy2(src_path, dst_path)

        file_entries.append({
            "name":   dst_name,
            "dest":   item["dest"],
            "url":    f"{GITHUB_RAW_BASE}/{dst_name}",
            "sha256": digest,
            "size":   size,
        })

    manifest = {
        "version":       new_version,
        "build_date":    build_date,
        "release_notes": new_notes,
        "files":         file_entries,
    }

    # Write version.json (goes to root of repo — this is what clients download)
    with open("version.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"\n✓ version.json written  (version {new_version})")
    
    if changed_count > 0:
        print(f"✓ {changed_count} file(s) updated in updates/ folder")
    else:
        print(f"ℹ No files changed — only version.json needs to be pushed")

    print("\nNext steps:")
    print("  1. Review version.json")
    if changed_count > 0:
        print(f"  2. git add version.json updates/  (or specific changed files)")
    else:
        print(f"  2. git add version.json  (no changed files to push)")
    print("  3. git commit -m \"Release v" + new_version + "\"")
    print("  4. git push")
    print("\nClients will pick up the update on next app start.\n")


if __name__ == "__main__":
    main()
