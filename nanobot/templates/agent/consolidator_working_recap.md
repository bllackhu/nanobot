Create a structured session checkpoint that another model will use to continue this conversation. Summarize only the messages above (and the previous checkpoint, if present). Do not continue the conversation.

Use this exact format:

## Goal
What the user is trying to accomplish in this session.

## Constraints and Preferences
- Requirements, style, language, and hard limits the user stated

## Progress
### Done
- Completed work

### In Progress
- Current work

### Blocked
- Blockers, if any

## Key Decisions
- **Decision**: rationale

## Next Steps
1. What should happen next

## Critical Context
- Names, IDs, numbers, last agreed answer gist, and pending asks needed to continue

Rules:
- Preserve exact names, numbers, IDs, and quoted decisions.
- If a <previous-checkpoint> block is present, merge it: keep still-true facts, drop stale ones, and emit a single updated checkpoint. Do not copy it verbatim.
- Do not dump tool logs, stack traces, source code, or raw message transcripts.
- Do not emit archive fact tags.
- Output the sections only. No preamble or closing commentary.
- If nothing useful can be recovered, output a Goal of "Unknown" and put any salvageable facts under Critical Context.
