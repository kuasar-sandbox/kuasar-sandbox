#!/usr/bin/env python3

import json
import os
from datasets import load_dataset

workdir = os.path.dirname(os.path.abspath(__file__)
        )
output_file = os.path.join(workdir, "prompts.jsonl")

os.makedirs(workdir, exist_ok=True)

print("Downloading CodeAlpaca prompt dataset...")

dataset = load_dataset(
    "HuggingFaceH4/CodeAlpaca_20K",
    split="train",
)

count = 0

with open(output_file, "w", encoding="utf-8") as f:
    for row in dataset:
        prompt = row["prompt"].strip()

        if not prompt:
            continue

        record = {
            "source": "CodeAlpaca_20K",
            "prompt": prompt,
        }

        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        count += 1

print(f"Saved {count} prompts")
print(f"Prompt file: {output_file}")
