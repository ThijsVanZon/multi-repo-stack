import os
import sys

sys.dont_write_bytecode = True

if not __package__:
    # Run as `python -I -B <checkout>/mrs`: import the package from its parent, not its own directory.
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path if os.path.abspath(entry or ".") != _here]
    sys.path.insert(0, os.path.dirname(_here))

from mrs.cli import main  # noqa: E402

sys.exit(main())
