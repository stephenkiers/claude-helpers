"""Entry point: python3 -m scripts.flow3 <subcommand>."""

import sys

from scripts.flow3 import cli

if __name__ == "__main__":
    sys.exit(cli.main())
