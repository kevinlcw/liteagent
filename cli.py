"""Interactive command-line interface."""

from __future__ import annotations

import argparse
import json

from .agent import Agent
from .db import ConversationStore
from .config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Native LiteAgent CLI")
    parser.add_argument("--conversation-id", help="Continue an existing conversation")
    parser.add_argument("--verbose", action="store_true", help="Show reasoning and tool events")
    args = parser.parse_args()
    # The CLI formats stream events itself; Agent.verbose would duplicate them.
    agent = Agent(verbose=False)
    conversation_id = args.conversation_id or ConversationStore.new_id()
    print(f"LiteAgent ready. conversation_id={conversation_id}")
    print("Commands: /new, /history, /conversations, /exit")
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye")
            return
        if not text:
            continue
        if text in {"/exit", "/quit"}:
            print("Bye")
            return
        if text == "/new":
            conversation_id = ConversationStore.new_id()
            print(f"New conversation: {conversation_id}")
            continue
        if text == "/history":
            for item in agent.store.display_history(conversation_id):
                print(f"{item['role']}> {item['content'] or item['tool_calls'] or ''}")
            continue
        if text == "/conversations":
            print(json.dumps(agent.store.list_conversations(), ensure_ascii=False, indent=2))
            continue
        try:
            active_line: str | None = None
            saw_content = False
            for event in agent.run_stream(text, conversation_id):
                event_type = event["type"]
                if event_type == "content_delta":
                    if active_line != "content":
                        if active_line is not None:
                            print()
                        print("assistant> ", end="", flush=True)
                        active_line = "content"
                    saw_content = True
                    print(event["text"], end="", flush=True)
                elif event_type == "reasoning_delta" and args.verbose:
                    if active_line != "reasoning":
                        if active_line is not None:
                            print()
                        print("[reasoning] ", end="", flush=True)
                        active_line = "reasoning"
                    print(event["text"], end="", flush=True)
                elif event_type == "tool_start":
                    if active_line is not None:
                        print()
                    print(f"[tool] {event['name']}({json.dumps(event['arguments'], ensure_ascii=False)})", flush=True)
                    active_line = None
                elif event_type == "tool_result" and args.verbose:
                    print(f"[tool result] {json.dumps(event['result'], ensure_ascii=False)}", flush=True)
                elif event_type == "final":
                    conversation_id = event["conversation_id"]
                    if not saw_content:
                        print(f"assistant> {event['content']}", end="")
                        active_line = "content"
            if active_line is not None:
                print()
        except Exception as exc:
            print(f"error> {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
