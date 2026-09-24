"""入口:python3 -m nightread <file.pdf>"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
