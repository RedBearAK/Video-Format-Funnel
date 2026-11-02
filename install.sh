#!/bin/sh
# Installs the 'vidfunnel' command from this clone: a thin wrapper around
# 'bin/vidfunnel --install-command' (HANDOFF.md section 6.9). Arguments are
# passed through, for example '--system' or '--yes'.

VERSION="20261102"

here=$(cd "$(dirname "$0")" && pwd -P)
exec "$here/bin/vidfunnel" --install-command "$@"

# End of File #
