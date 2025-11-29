#!/bin/bash

# Script to merge folders without overwriting existing files
# Uses mv for speed (just updates inodes instead of copying data)

usage() {
    cat << EOF
Usage: $0 -s SOURCE -d DESTINATION [-n] [-r] [-v] [-h]

Options:
    -s SOURCE       Source folder to move from
    -d DESTINATION  Destination folder to merge into
    -n              Dry run mode (show what would be done without doing it)
    -r              Rename conflicts (add .1, .2, etc.) instead of skipping
                    Files with identical content will still be skipped
    -v              Verbose mode (print every file operation)
    -h              Show this help message

Example:
    $0 -s /path/to/source -d /path/to/destination
    $0 -s /path/to/source -d /path/to/destination -n  # dry run
    $0 -s /path/to/source -d /path/to/destination -r  # rename conflicts
    $0 -s /path/to/source -d /path/to/destination -v  # verbose output

EOF
    exit 1
}

# Function to find next available numbered filename
find_available_name() {
    local base_path="$1"
    local dir=$(dirname "$base_path")
    local filename=$(basename "$base_path")
    local name="${filename%.*}"
    local ext="${filename##*.}"
    
    # Handle files without extension
    if [ "$name" = "$ext" ]; then
        ext=""
    else
        ext=".$ext"
    fi
    
    local counter=1
    local new_path
    
    while true; do
        new_path="$dir/${name}.${counter}${ext}"
        if [ ! -f "$new_path" ]; then
            echo "$new_path"
            return
        fi
        ((counter++))
    done
}

# Function to compare two files
files_are_identical() {
    local file1="$1"
    local file2="$2"
    
    # Use cmp for binary-safe comparison (faster than diff for this purpose)
    cmp -s "$file1" "$file2"
    return $?
}

# Parse command line options
DRY_RUN=false
RENAME_CONFLICTS=false
VERBOSE=false
SOURCE=""
DESTINATION=""

while getopts "s:d:nrvh" opt; do
    case $opt in
        s)
            SOURCE="$OPTARG"
            ;;
        d)
            DESTINATION="$OPTARG"
            ;;
        n)
            DRY_RUN=true
            ;;
        r)
            RENAME_CONFLICTS=true
            ;;
        v)
            VERBOSE=true
            ;;
        h)
            usage
            ;;
        \?)
            echo "Invalid option: -$OPTARG" >&2
            usage
            ;;
        :)
            echo "Option -$OPTARG requires an argument." >&2
            usage
            ;;
    esac
done

# Check if required parameters are provided
if [ -z "$SOURCE" ] || [ -z "$DESTINATION" ]; then
    echo "Error: Source and destination folders are required."
    usage
fi

# Validate source folder exists
if [ ! -d "$SOURCE" ]; then
    echo "Error: Source folder '$SOURCE' does not exist."
    exit 1
fi

# Create destination if it doesn't exist
if [ ! -d "$DESTINATION" ]; then
    if [ "$DRY_RUN" = true ]; then
        echo "[DRY RUN] Would create destination folder: $DESTINATION"
    else
        mkdir -p "$DESTINATION"
        echo "Created destination folder: $DESTINATION"
    fi
fi

# Remove trailing slashes for consistency
SOURCE="${SOURCE%/}"
DESTINATION="${DESTINATION%/}"

echo "========================================="
if [ "$DRY_RUN" = true ]; then
    echo "DRY RUN MODE - No files will be moved"
fi
if [ "$RENAME_CONFLICTS" = true ]; then
    echo "RENAME MODE - Conflicts will be renamed"
fi
if [ "$VERBOSE" = true ]; then
    echo "VERBOSE MODE - Showing all operations"
fi
echo "Source:      $SOURCE"
echo "Destination: $DESTINATION"
echo "========================================="
if [ "$VERBOSE" = true ]; then
    echo ""
fi

# Arrays to track results
declare -a moved_files
declare -a skipped_files
declare -a renamed_files
declare -a identical_files
moved_count=0
skipped_count=0
renamed_count=0
identical_count=0
total_files=0

# Process all files
while IFS= read -r -d '' file; do
    ((total_files++))
    
    # Get relative path from source
    rel_path="${file#$SOURCE/}"
    target="$DESTINATION/$rel_path"
    
    # Check if target file already exists
    if [ -f "$target" ]; then
        # Check if files are identical
        if files_are_identical "$file" "$target"; then
            identical_files+=("$rel_path")
            ((identical_count++))
            if [ "$VERBOSE" = true ]; then
                echo "[IDENTICAL] $rel_path (same content, left in source)"
            fi
        elif [ "$RENAME_CONFLICTS" = true ]; then
            # Find available numbered name
            new_target=$(find_available_name "$target")
            new_rel_path="${new_target#$DESTINATION/}"
            renamed_files+=("$rel_path -> $new_rel_path")
            ((renamed_count++))
            
            if [ "$VERBOSE" = true ]; then
                if [ "$DRY_RUN" = true ]; then
                    echo "[WOULD RENAME] $rel_path -> $new_rel_path"
                else
                    echo "[RENAMED] $rel_path -> $new_rel_path"
                fi
            fi
            
            if [ "$DRY_RUN" = false ]; then
                # Create target directory if needed
                target_dir="$(dirname "$new_target")"
                mkdir -p "$target_dir"
                
                # Move with new name
                mv "$file" "$new_target"
            fi
        else
            skipped_files+=("$rel_path")
            ((skipped_count++))
            if [ "$VERBOSE" = true ]; then
                if [ "$DRY_RUN" = true ]; then
                    echo "[SKIP] $rel_path (already exists, different content)"
                fi
            fi
        fi
    else
        moved_files+=("$rel_path")
        ((moved_count++))
        
        if [ "$VERBOSE" = true ]; then
            if [ "$DRY_RUN" = true ]; then
                echo "[WOULD MOVE] $rel_path"
            else
                echo "[MOVED] $rel_path"
            fi
        fi
        
        if [ "$DRY_RUN" = false ]; then
            # Create target directory if needed
            target_dir="$(dirname "$target")"
            mkdir -p "$target_dir"
            
            # Move the file
            mv "$file" "$target"
        fi
    fi
done < <(find "$SOURCE" -type f -print0)

# Print summary
echo ""
echo "========================================="
echo "SUMMARY"
echo "========================================="

# Determine if we should show detailed lists (verbose mode OR <= 50 total special files)
total_special=$((identical_count + renamed_count + skipped_count))
show_details=false

if [ "$VERBOSE" = true ] || [ $total_special -le 50 ]; then
    show_details=true
fi

if [ $identical_count -gt 0 ]; then
    echo ""
    echo "Files with identical content (skipped): $identical_count"
    if [ "$show_details" = true ]; then
        echo "-----------------------------------------"
        for identical in "${identical_files[@]}"; do
            echo "  - $identical"
        done
    fi
fi

if [ $renamed_count -gt 0 ]; then
    echo ""
    echo "Files renamed due to conflicts: $renamed_count"
    if [ "$show_details" = true ]; then
        echo "-----------------------------------------"
        for renamed in "${renamed_files[@]}"; do
            echo "  - $renamed"
        done
    fi
fi

if [ $skipped_count -gt 0 ]; then
    echo ""
    echo "Files NOT moved (conflicts, different content): $skipped_count"
    if [ "$show_details" = true ]; then
        echo "-----------------------------------------"
        for skipped in "${skipped_files[@]}"; do
            echo "  - $skipped"
        done
    fi
fi

if [ "$show_details" = false ]; then
    echo ""
    echo "(File lists hidden - more than 50 files. Use -v for verbose output)"
fi

echo ""
echo "========================================="
echo "RECAP"
echo "========================================="
echo "Total files found:    $total_files"
echo "Files moved:          $moved_count"
echo "Files renamed:        $renamed_count"
echo "Identical (skipped):  $identical_count"
echo "Files skipped:        $skipped_count"

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "This was a DRY RUN - no files were actually moved."
    echo "Run without -n flag to perform the actual move."
else
    # Check if source folder is now empty (except for empty directories)
    remaining=$(find "$SOURCE" -type f | wc -l)
    if [ "$remaining" -eq 0 ]; then
        echo ""
        echo "Source folder contains no more files."
        echo "You can safely remove it with: rm -rf '$SOURCE'"
    else
        echo ""
        echo "Warning: $remaining files remain in source folder."
    fi
fi

echo "========================================="
