#!/bin/bash

# Parse command line arguments
THRESHOLD_MB=${1:-512}  # Default to 512MB if no argument provided
THRESHOLD_KB=$((THRESHOLD_MB * 1024))  # Convert MB to KB

# Set check interval in seconds (default: 60 seconds)
CHECK_INTERVAL=${CHECK_INTERVAL:-60}

# Create log directory if it doesn't exist
LOG_DIR="$HOME/log/spectrumx"
mkdir -p "$LOG_DIR"

# Generate log filename with ISO datetime
DATETIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
LOG_FILE="$LOG_DIR/system_mem_guard_${DATETIME}.log"

# Function to log messages
log_message() {
    local message="$1"
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] $message" >> "$LOG_FILE"
    echo "[$timestamp] $message"
}

# Function to check and kill high-memory processes
check_and_kill_processes() {
    local killed_count=0
    local checked_count=0
    
    # Get list of PIDs and their RSS memory (in KB), sorted by usage
    # Use a temporary file to avoid subshell issues
    TEMP_FILE=$(mktemp)
    ps -eo pid,cmd,rss --sort=-rss | tail -n +2 > "$TEMP_FILE"

    # Process each line - use awk to properly parse the fields
    while IFS= read -r line; do
        checked_count=$((checked_count + 1))
        
        # Extract PID, RSS, and command using awk
        pid=$(echo "$line" | awk '{print $1}')
        rss=$(echo "$line" | awk '{print $(NF)}')  # Last field is RSS
        cmd=$(echo "$line" | awk '{$1=""; $(NF)=""; print substr($0, 2)}')  # Remove first and last fields
        
        # Skip if we couldn't parse the line properly
        if [[ ! "$rss" =~ ^[0-9]+$ ]] || [[ ! "$pid" =~ ^[0-9]+$ ]]; then
            continue
        fi
        
        if [ "$rss" -gt "$THRESHOLD_KB" ]; then
            # Only kill python processes
            if [[ "$cmd" == *"python"* ]]; then
                log_message "Killing process (PID $pid) for using $rss KB"
                log_message "Command: $cmd"
                
                # Try to kill the process
                if kill -9 "$pid" 2>/dev/null; then
                    killed_count=$((killed_count + 1))
                    log_message "Successfully killed process (PID $pid)"
                else
                    log_message "Failed to kill process (PID $pid) - process may have already terminated"
                fi
            fi
        fi
    done < "$TEMP_FILE"

    # Clean up temporary file
    rm -f "$TEMP_FILE"
    
    # Only log summary if processes were killed
    if [ "$killed_count" -gt 0 ]; then
        log_message "Check completed - checked $checked_count processes, killed $killed_count Python processes"
    fi
}

# Log script start
log_message "System memory guard script started (continuous mode)"
log_message "Memory threshold: ${THRESHOLD_MB} MB (${THRESHOLD_KB} KB)"
log_message "Check interval: ${CHECK_INTERVAL} seconds"
log_message "Log file: $LOG_FILE"

# Main loop - run continuously
while true; do
    check_and_kill_processes
    sleep "$CHECK_INTERVAL"
done