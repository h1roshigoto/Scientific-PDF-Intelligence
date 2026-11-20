"""Record explicit publication scope decisions for imported PDF versions."""

import argparse
from pathlib import Path

from pydantic import TypeAdapter

from .models import PublicationScope
from .storage import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON array of scope decisions reviewed against the PDFs")
    parser.add_argument("--state", default="state")
    args = parser.parse_args()
    try:
        decisions = TypeAdapter(list[PublicationScope]).validate_json(args.manifest.read_text(encoding="utf-8-sig"))
        Store(args.state).save_publication_scopes(decisions)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    for decision in decisions:
        print(f"{decision.document_id}: {decision.status}")


if __name__ == "__main__":
    main()
