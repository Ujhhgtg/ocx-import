"""Import ChatGPT Web Session and sub2api JSON into OpenCodex."""

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as cli_main

    return cli_main()
