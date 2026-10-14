#!/bin/bash
# Remove unnecessary indexes from DuckDB schema files
# DuckDB is a columnar OLAP database - secondary indexes are rarely needed!

echo "========================================================================"
echo "  Removing Unnecessary Indexes from Schema Files"
echo "========================================================================"
echo ""
echo "DuckDB uses columnar storage with automatic zone maps and statistics."
echo "Secondary indexes add overhead without performance benefit for OLAP queries."
echo ""

SCHEMA_DIR="database/schema"
BACKUP_DIR="database/schema_backup_$(date +%Y%m%d_%H%M%S)"

echo "📁 Creating backup: $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp -r "$SCHEMA_DIR"/*.sql "$BACKUP_DIR/"
echo "   ✅ Backup created"
echo ""

echo "🔧 Removing index definitions from schema files..."
echo ""

for file in "$SCHEMA_DIR"/*.sql; do
    filename=$(basename "$file")

    # Count indexes before
    before=$(grep -c "^CREATE INDEX" "$file" || echo "0")

    if [ "$before" -gt 0 ]; then
        echo "📄 $filename"
        echo "   Before: $before indexes"

        # Remove lines that start with "CREATE INDEX"
        # Keep everything else (including PRIMARY KEY definitions)
        sed -i.tmp '/^CREATE INDEX/d' "$file"

        # Also remove any blank lines that were left behind (optional cleanup)
        sed -i.tmp '/^$/N;/^\n$/d' "$file"

        # Remove the .tmp backup created by sed
        rm -f "${file}.tmp"

        after=$(grep -c "^CREATE INDEX" "$file" || echo "0")
        echo "   After:  $after indexes"
        echo "   ✅ Removed $before indexes"
    else
        echo "📄 $filename - No indexes to remove"
    fi
    echo ""
done

echo "========================================================================"
echo "  ✅ Index Removal Complete!"
echo "========================================================================"
echo ""
echo "Summary:"
echo "  - Original files backed up to: $BACKUP_DIR"
echo "  - PRIMARY KEY definitions preserved"
echo "  - All CREATE INDEX statements removed"
echo ""
echo "Next steps:"
echo "  1. Review the changes: git diff $SCHEMA_DIR"
echo "  2. Test with fresh database:"
echo "     rm data/vlml_events.duckdb"
echo "     python database/scripts/orchestration/run_pipeline.py --year 2025"
echo "  3. Measure performance improvement"
echo ""
echo "To restore from backup:"
echo "  cp $BACKUP_DIR/*.sql $SCHEMA_DIR/"
echo ""
