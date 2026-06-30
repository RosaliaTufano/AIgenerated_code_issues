from datetime import datetime, timedelta
from pathlib import Path
import re

import pandas as pd
from gharchive import GHArchive
from tqdm import tqdm


# Configuration
YEAR = 2022
START = datetime(YEAR, 4, 30, 23)  # first processed hour will be 2022-05-01-00
OUTPUT_DIR = Path("./data")
OUTPUT_SUFFIX = "relevant_commit_messages_found.csv"

# regex: AI-assistant terms
AI_TOOL_PATTERN = r"""
(?:github\s*co-?pilot
  |copilot(?!\s*(studio|m365))
  |chatgpt
  |gpt-?4(?:\.\d+)?
  |gpt-?5(?:\.\d+)?
  |gpt-?3\.5
  |openai
  |claude
  |anthropic
  |gemini
  |bard
  |code\s*whisperer
  |tabnine
  |llm
  |ai\s+(?:assistant|suggestion|generated)
  |aider
  |cursor
  |codex
  |coderabbit
  |cline
  |devin
  |kiro
  |openhands
  |roo\ code
  |sweep
)
"""

pattern = re.compile(
    rf"""\b
    (?:fix|correct|address|patch|hotfix|revert|undo|rollback|back\ out)\w*
    .{{0,10}}\b
    (?:bug|issue|regression|mistake|error|broken|breaks|build)\w*
    .{{0,10}}\b
    (?:introduced|caused|from|due\ to|by|generated\ by|suggested\ by)\w*
    .{{0,20}}\b
    {AI_TOOL_PATTERN}
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)


def save_month(year: int, month: str, events: list, commits: list, messages: list) -> None:
    """Save one month's matched commits."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{year}_{month}_{OUTPUT_SUFFIX}"
    output_path = OUTPUT_DIR / filename
    pd.DataFrame(
        {
            "event": events,
            "commit": commits,
            "message": messages,
        }
    ).to_csv(output_path, index=False)
    print(f"elements found for {year}_{month}: {len(events)}")
    print(f"saved: {output_path}")


def main() -> None:
    gh = GHArchive()

    relevant_events = []
    relevant_commits = []
    relevant_messages = []

    t = START
    current_month = f"{START.month + 1:02d}"  # because START is the hour before the first target month

    while True:
        next_t = t + timedelta(hours=1)
        formatted = next_t.strftime("%Y-%m-%d-%H")
        print(formatted)

        year, month, *_ = formatted.split("-")

        if month != current_month:
            save_month(YEAR, current_month, relevant_events, relevant_commits, relevant_messages)
            current_month = month
            relevant_events = []
            relevant_commits = []
            relevant_messages = []

        if year != str(YEAR):
            break

        t = next_t
        try:
            archive = gh.get(formatted, filters=[("type", "PushEvent")])
            dicts = archive.to_dict_list()
        except Exception as exc:
            print(f"skipping {formatted}: {exc}")
            continue

        for current_dict in tqdm(dicts):
            commits = current_dict.get("payload", {}).get("commits") or []
            for commit in commits:
                message = commit.get("message", "")
                if pattern.search(message):
                    relevant_events.append(current_dict)
                    relevant_commits.append(commit)
                    relevant_messages.append(message)

        print(f"all event found: {len(dicts)}")


if __name__ == "__main__":
    main()
