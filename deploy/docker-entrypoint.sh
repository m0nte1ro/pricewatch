#!/bin/sh
set -eu
umask 077

PRICEWATCH_DATA_DIR="${PRICEWATCH_DATA_DIR:-/var/lib/pricewatch}"
export PRICEWATCH_DATA_DIR
mkdir -p "$PRICEWATCH_DATA_DIR"

if [ "$(id -u)" = 0 ]; then
    # Compose creates a missing bind directory as root. Only application data needs ownership.
    find "$PRICEWATCH_DATA_DIR" -xdev -exec chown -h pricewatch:pricewatch {} +
    chmod 0700 "$PRICEWATCH_DATA_DIR"
    exec gosu pricewatch "$0" "$@"
fi

if [ ! -w "$PRICEWATCH_DATA_DIR" ]; then
    printf '%s\n' "Pricewatch cannot write to $PRICEWATCH_DATA_DIR; check bind-mount ownership." >&2
    exit 1
fi

printf '%s\n' 'Applying Pricewatch database migrations…'
python -m app.migrate
printf '%s\n' 'Starting Pricewatch…'
exec "$@"
