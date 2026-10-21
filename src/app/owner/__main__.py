"""`uv run python -m app.owner off | on | status`: the bot ON / OFF button from the shell."""

from app.owner.presence import main

if __name__ == "__main__":
    main()
