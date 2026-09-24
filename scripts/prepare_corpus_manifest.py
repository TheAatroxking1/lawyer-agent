"""Thin entrypoint; run with the backend environment installed for this repository."""

from lawyer_agent.cli.corpus_manifest import main

if __name__ == "__main__":
    raise SystemExit(main())
