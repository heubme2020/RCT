#!/bin/bash
set -euo pipefail
install -d -m 0700 /var/backups/rct
sqlite3 /var/lib/rct/rct_clean.db ".backup '/var/backups/rct/rct-$(date +%F-%H%M%S).db'"
find /var/backups/rct -type f -name 'rct-*.db' -mtime +30 -delete
