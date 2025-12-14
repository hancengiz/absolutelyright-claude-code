#!/usr/bin/env python3
"""Watcher for user prompt messages - tracks words in user prompts to Claude."""
import os
import sys
import json
import time
from pathlib import Path
from datetime import datetime

from word_counter import (
    CLAUDE_PROJECTS_BASE,
    DATA_DIR,
    SERVER_URL,
    TRACKED_WORDS,
    ensure_data_dir,
    get_utc_today,
    get_project_display_name,
    process_user_message_entry,
    upload_to_api,
)
import re

# Additional data files for watcher
WORD_COUNTS_FILE = os.path.join(DATA_DIR, "word_counts.json")
PROCESSED_IDS_FILE = os.path.join(DATA_DIR, "processed_ids.json")
TOTAL_MESSAGES_FILE = os.path.join(DATA_DIR, "daily_total_user_messages.json")


def load_processed_ids():
    """Load set of already processed message IDs"""
    if os.path.exists(PROCESSED_IDS_FILE):
        try:
            with open(PROCESSED_IDS_FILE, "r") as f:
                return set(json.load(f))
        except:
            pass
    return set()


def save_processed_ids(ids_set):
    """Save processed message IDs"""
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(list(ids_set), f)


def load_word_counts(word_name):
    """Load daily counts for a specific word"""
    filename = os.path.join(DATA_DIR, f"daily_{word_name}_counts.json")
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_word_counts(word_name, counts):
    """Save daily counts for a specific word"""
    filename = os.path.join(DATA_DIR, f"daily_{word_name}_counts.json")
    with open(filename, "w") as f:
        json.dump(counts, f, indent=2)


def load_total_messages_counts():
    """Load daily counts of total user messages"""
    if os.path.exists(TOTAL_MESSAGES_FILE):
        try:
            with open(TOTAL_MESSAGES_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_total_messages_counts(counts):
    """Save daily counts of total user messages"""
    with open(TOTAL_MESSAGES_FILE, "w") as f:
        json.dump(counts, f, indent=2)


def backfill_today_total_messages():
    """Scan all projects and count today's total user messages (deduplicated)"""
    today_utc = get_utc_today()
    seen_message_ids = set()

    if not os.path.exists(CLAUDE_PROJECTS_BASE):
        return 0

    print(f"Backfilling today's ({today_utc}) total user message count...")

    for project_dir in Path(CLAUDE_PROJECTS_BASE).iterdir():
        if project_dir.is_dir() and not project_dir.name.startswith("."):
            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    with open(jsonl_file, "r") as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                                if entry.get("type") == "user":
                                    msg_id = entry.get("uuid") or entry.get("requestId")
                                    if not msg_id:
                                        continue

                                    timestamp = entry.get("timestamp", "")
                                    if timestamp:
                                        entry_time = datetime.fromisoformat(
                                            timestamp.replace("Z", "+00:00")
                                        )
                                        date_str = entry_time.strftime("%Y-%m-%d")
                                        if date_str == today_utc and msg_id not in seen_message_ids:
                                            seen_message_ids.add(msg_id)
                            except:
                                continue
                except:
                    pass

    return len(seen_message_ids)


def backfill_today_words(compiled_tracked, processed_ids):
    """Scan all projects for today's word matches and mark them as processed"""
    today_utc = get_utc_today()
    word_matches = {name: 0 for name in TRACKED_WORDS}
    seen_today = set()  # Track messages seen during this backfill to avoid duplicates

    if not os.path.exists(CLAUDE_PROJECTS_BASE):
        return word_matches

    print(f"Backfilling today's ({today_utc}) word matches...")

    for project_dir in Path(CLAUDE_PROJECTS_BASE).iterdir():
        if project_dir.is_dir() and not project_dir.name.startswith("."):
            for jsonl_file in project_dir.glob("*.jsonl"):
                try:
                    with open(jsonl_file, "r") as f:
                        for line in f:
                            try:
                                entry = json.loads(line)
                                result = process_user_message_entry(entry, compiled_tracked)

                                if not result:
                                    continue

                                msg_id = result["msg_id"]
                                date_str = result["date_str"]

                                # Only process today's messages
                                if date_str != today_utc:
                                    continue

                                # Skip if already counted in this backfill (deduplication)
                                if msg_id in seen_today:
                                    continue

                                seen_today.add(msg_id)

                                # Mark as processed for the main loop
                                processed_ids.add(msg_id)

                                # Process text blocks for word matches (count once per message)
                                message_words = set()
                                for text, matched_words in result["text_blocks"]:
                                    message_words.update(matched_words.keys())

                                for word_name in message_words:
                                    word_matches[word_name] += 1

                            except:
                                continue
                except:
                    pass

    return word_matches


def main():
    """Main watcher loop"""
    ensure_data_dir()

    # Use config server URL by default, allow command line override
    api_url = SERVER_URL
    api_secret = None

    for i, arg in enumerate(sys.argv):
        if arg == "--upload" and i + 1 < len(sys.argv):
            api_url = sys.argv[i + 1]
            if i + 2 < len(sys.argv) and not sys.argv[i + 2].startswith("--"):
                api_secret = sys.argv[i + 2]
            break
        elif arg == "--secret" and i + 1 < len(sys.argv):
            api_secret = sys.argv[i + 1]

    print("Prompt Words Watcher")
    print("=" * 50)
    print(f"Watching: {CLAUDE_PROJECTS_BASE}")
    print(f"Data directory: {DATA_DIR}")
    print("Tracking words:")
    for name, pattern in TRACKED_WORDS.items():
        print(f"  {name}: {pattern}")
    if api_url:
        print(f"API URL: {api_url}")
    print("-" * 50)

    # Compile patterns
    compiled_tracked = {
        name: re.compile(pattern, re.IGNORECASE) for name, pattern in TRACKED_WORDS.items()
    }

    # Initialize
    processed_ids = load_processed_ids()
    word_counts = {name: load_word_counts(name) for name in TRACKED_WORDS}
    total_messages_counts = load_total_messages_counts()

    # Backfill today's total message count on startup
    today_utc = get_utc_today()
    today_total_actual = backfill_today_total_messages()
    total_messages_counts[today_utc] = today_total_actual
    save_total_messages_counts(total_messages_counts)
    print(f"Found {today_total_actual} total user messages for today")

    # Backfill today's word matches on startup (replaces today's counts)
    backfill_word_matches = backfill_today_words(compiled_tracked, processed_ids)
    for word_name, count in backfill_word_matches.items():
        if count > 0:
            word_counts[word_name][today_utc] = count  # SET, not ADD
            save_word_counts(word_name, word_counts[word_name])
            print(f"Found {count} '{word_name}' matches for today")

    # Save processed IDs after backfill
    save_processed_ids(processed_ids)

    # Upload today's data on startup if API is configured
    if api_url:
        today_utc = get_utc_today()
        today_local = datetime.now().strftime("%Y-%m-%d")

        # Collect all word counts for today
        today_words = {name: counts.get(today_utc, 0) for name, counts in word_counts.items()}
        today_total = total_messages_counts.get(today_utc, 0)

        timezone_note = ""
        if today_utc != today_local:
            timezone_note = f" (UTC {today_utc}, local {today_local})"

        words_summary = ", ".join([f"{name}={count}" for name, count in today_words.items()])
        print(
            f"Uploading today's counts{timezone_note}: {words_summary}, total_user_messages={today_total}"
        )
        if upload_to_api(api_url, api_secret, today_utc, words_dict=today_words, total_user_messages=today_total):
            print("  ✓ Upload successful")
        else:
            print("  ✗ Upload failed")

    print("-" * 50)

    if not os.path.exists(CLAUDE_PROJECTS_BASE):
        print(f"Error: Claude projects directory not found at {CLAUDE_PROJECTS_BASE}")
        print("Set CLAUDE_PROJECTS environment variable to your Claude projects path")
        return

    try:
        while True:
            new_matches_by_word = {name: 0 for name in TRACKED_WORDS}
            new_total_messages = 0

            for project_dir in Path(CLAUDE_PROJECTS_BASE).iterdir():
                if project_dir.is_dir() and not project_dir.name.startswith("."):
                    project_name = get_project_display_name(project_dir.name)

                    # Scan all JSONL files in this project
                    for jsonl_file in project_dir.glob("*.jsonl"):
                        # Single pass: count total messages and check for word matches
                        try:
                            with open(jsonl_file, "r") as f:
                                for line in f:
                                    try:
                                        entry = json.loads(line)
                                        result = process_user_message_entry(entry, compiled_tracked)

                                        if not result:
                                            continue

                                        msg_id = result["msg_id"]
                                        date_str = result["date_str"]

                                        if msg_id in processed_ids:
                                            continue

                                        # Mark as processed
                                        processed_ids.add(msg_id)

                                        # Update total messages count
                                        if date_str not in total_messages_counts:
                                            total_messages_counts[date_str] = 0
                                        total_messages_counts[date_str] += 1
                                        new_total_messages += 1

                                        # Process text blocks for word matches (count once per message)
                                        message_words = set()
                                        first_match_text = None
                                        for text, matched_words in result["text_blocks"]:
                                            if matched_words:
                                                message_words.update(matched_words.keys())
                                                if first_match_text is None:
                                                    first_match_text = text

                                        if message_words:
                                            for word_name in message_words:
                                                new_matches_by_word[word_name] += 1

                                                # Update daily counts
                                                if date_str not in word_counts[word_name]:
                                                    word_counts[word_name][date_str] = 0
                                                word_counts[word_name][date_str] += 1

                                            # Print notification (once per message)
                                            match_types = list(message_words)
                                            print(
                                                f"[{datetime.now().strftime('%H:%M:%S')}] {', '.join(match_types).upper()} in {project_name}: {first_match_text.strip()[:100]}"
                                            )

                                    except:
                                        continue
                        except:
                            pass

            if any(new_matches_by_word.values()) or new_total_messages > 0:
                # Save all state
                save_processed_ids(processed_ids)
                for word_name, counts in word_counts.items():
                    save_word_counts(word_name, counts)
                save_total_messages_counts(total_messages_counts)

                updates = [
                    f"{name}: +{count}"
                    for name, count in new_matches_by_word.items()
                    if count > 0
                ]
                if new_total_messages > 0:
                    updates.append(f"total_user_messages: +{new_total_messages}")
                print(f"Updated: {', '.join(updates)}")

                # Upload to API if configured
                if api_url:
                    today_utc = get_utc_today()
                    today_words = {name: counts.get(today_utc, 0) for name, counts in word_counts.items()}
                    today_total = total_messages_counts.get(today_utc, 0)
                    if upload_to_api(
                        api_url, api_secret, today_utc, words_dict=today_words, total_user_messages=today_total
                    ):
                        words_summary = ", ".join([f"{name}={count}" for name, count in today_words.items()])
                        print(
                            f"  ✓ Uploaded to API: {words_summary}, total_user_messages={today_total}"
                        )

            time.sleep(int(os.environ.get("CHECK_INTERVAL", "2")))

    except KeyboardInterrupt:
        print("\n" + "-" * 50)
        print("Stopping watcher...")
        for name in TRACKED_WORDS:
            total = sum(word_counts[name].values())
            print(f"Final '{name}' count: {total}")


if __name__ == "__main__":
    main()
