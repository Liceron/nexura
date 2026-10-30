# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Liceron
"""Allow running python -m nexura_runtime.modules directly."""

import sys

from nexura_runtime.modules.cli import main

if __name__ == "__main__":
    sys.exit(main())
