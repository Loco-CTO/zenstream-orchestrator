#!/bin/sh
set -eu

metadata_path="${METADATA_PATH:-/app/sqlite}"

case "$metadata_path" in
    ""|/|/app|/etc|/home|/root|/tmp|/usr|/var)
        echo "Refusing unsafe METADATA_PATH for ownership repair: $metadata_path" >&2
        exit 1
        ;;
esac

database_path="$metadata_path/orchestrator.db"

metadata_is_writable() {
    gosu zenstream test -w "$metadata_path" &&
        { [ ! -e "$database_path" ] || gosu zenstream test -w "$database_path"; }
}

if [ "$(id -u)" -eq 0 ]; then
    mkdir -p "$metadata_path"

    if ! metadata_is_writable; then
        echo "Repairing metadata ownership for the non-root ZenStream runtime: $metadata_path"
        chown -R zenstream:zenstream "$metadata_path"
        gosu zenstream chmod -R u+rwX "$metadata_path"
    fi

    if ! metadata_is_writable; then
        echo "METADATA_PATH is not writable by the ZenStream runtime user: $metadata_path" >&2
        echo "Ensure the container mount is read-write and the host path permits writes." >&2
        exit 1
    fi

    exec gosu zenstream "$@"
fi

if [ ! -d "$metadata_path" ] || [ ! -w "$metadata_path" ] ||
    { [ -e "$database_path" ] && [ ! -w "$database_path" ]; }; then
    echo "METADATA_PATH is not writable by uid $(id -u): $metadata_path" >&2
    echo "Run the image with its default entrypoint user or repair the host mount permissions." >&2
    exit 1
fi

exec "$@"
