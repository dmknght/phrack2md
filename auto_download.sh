#!/usr/bin/env bash
set -euo pipefail

# Download the official Phrack archive.
# The tarballs are stored under ./tgz/
URL="https://archives.phrack.org/tgz/"

wget -r \
    -nH \
    --no-parent \
    --reject="index.html*" \
    -e robots=off \
    "$URL"

# Directory containing extracted issues
ISSUES_DIR="issues"

mkdir -p "$ISSUES_DIR"

# Extract each Phrack issue and generate a PDF.
for tarball in tgz/phrack*.tar.gz; do
    issue=$(basename "$tarball" .tar.gz)
    issue_dir="$ISSUES_DIR/$issue"

    echo "Processing $issue..."

    mkdir -p "$issue_dir"

    # Extract the issue archive.
    tar xzf "$tarball" -C "$issue_dir"

done

echo "Done."