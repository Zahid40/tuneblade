"""PyInstaller entry point (the package can't be frozen via `-m`)."""

from tuneblade.__main__ import main

if __name__ == "__main__":
    main()
