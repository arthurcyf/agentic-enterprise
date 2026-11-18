# How this file fits in
# ---------------------
# Used by:  `python -m audit ...` (and the Makefile targets).
# Calls:    audit.cli.main(), which dispatches to every host-side command.

from audit.cli import main

if __name__ == "__main__":
    main()
