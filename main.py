#!/usr/bin/env python3
"""
NRTV Video Agent Network
Entry point for job submission.

Usage:
    python main.py --type script_brief --content "15s ad for Rugiet Ready" --brand brands/rugiet.yaml
    python main.py --type voice_memo --content /path/to/memo.m4a
    python main.py --type revision --content "make the logo bigger" --concept-id NTV-001

Test mode (no external API costs):
    TEST_MODE=true python main.py --type script_brief --content "test job"
"""

import argparse
import json
import os

from packs.video.head_of_production import HeadOfProduction


def main():
    parser = argparse.ArgumentParser(description="NRTV Video Agent Network")
    parser.add_argument(
        "--type",
        required=True,
        choices=["script_brief", "still_variations", "revision", "voice_memo", "broll_edit"],
    )
    parser.add_argument("--content", required=True, help="Job content (text brief or file path)")
    parser.add_argument("--brand", help="Path to brand YAML file")
    parser.add_argument("--concept-id", help="Existing concept ID (e.g. NTV-001)")
    args = parser.parse_args()

    test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
    if test_mode:
        print("TEST MODE ACTIVE — no external API calls will be made\n")

    job = {
        "type": args.type,
        "content": args.content,
        "brand_file": args.brand,
        "concept_id": args.concept_id,
        "test_mode": test_mode,
    }

    hop = HeadOfProduction()
    try:
        result = hop.receive_job(job)
    except Exception as e:
        print(f"\n[ERROR] Job failed: {e}")
        raise

    print("\n" + "=" * 60)
    print("JOB COMPLETE")
    print("=" * 60)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
