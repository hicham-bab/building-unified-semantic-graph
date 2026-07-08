#!/usr/bin/env bash
# Install this repo as a dbt Wizard skill.
set -euo pipefail

DEST="${DBT_WIZARD_SKILLS_DIR:-$HOME/.dbt/wizard/skills}/building-unified-semantic-graph"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$DEST"
cp -R "$SRC/SKILL.md" "$SRC/scripts" "$SRC/references" "$DEST/"

echo "Installed building-unified-semantic-graph -> $DEST"
echo "Open dbt Wizard in a project and ask it to build a unified semantic knowledge graph."
