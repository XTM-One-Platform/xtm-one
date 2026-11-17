# Contract: Decision Request (System One)

One decision is one `flow_classifier.classify()` call (research R5), sent by `decide_choice()` in the provider's wire format. For Ollama that is `POST /v1/systemone`:

```json
{
  "model": "clef-flash",
  "state": "Goal: search the observables for apt28\nText to type: apt28\nDone so far: navigate /dashboard/observations\nPage:\n<snapshot text, at most 12000 characters>",
  "questions": {
    "label": {
      "type": "choice",
      "instructions": "Which action brings the page closer to the goal? Choose impossible if no action helps.",
      "criteria": {
        "click:e2": "click link \"Analyses\" (menu) -> /dashboard/analyses",
        "open:e2": "open the menu of link \"Analyses\" (menu) -> /dashboard/analyses",
        "type:e7": "type the text into searchbox \"Search\" and press Enter",
        "impossible": "no action helps"
      }
    }
  }
}
```

- Each option describes its element with its whole snapshot line: role, name, states, value and, for a link, its destination.
- `Text to type` is present only while the agent's `text` is untyped. `Done so far` lists the run's steps (`click link "Reports"; type textbox "Search"`), absent before the first.
- A question has at most 16 options. A page with more than 15 is asked in groups of 15 plus `none` ("none of these actions helps"), then again over the groups' picks until they fit one question with `impossible`.

Each step asks up to three questions, in this order, on the same state:

1. **Reached**: "Is the goal reached, given what was done so far and the page as it is now?", choices `done` / `not_yet`. Asked first and apart from the actions: offered as one more option among them, `done` lost to the next step button page after page.
2. **Action**: the request above.
3. **Commit check**, before any action (`click`, `open`, `type`) when the agent did not pass `submit` - `open` clicks a menu that does not open on hover, and `type` presses Enter in a form of that one field: "Would this action save, create, send, submit or delete something, or does it only move through the page?", choices `commits` / `moves`, with `Action: …` (the chosen option) in the state. The page is what tells: both decision models judged a plain "Create agent" button committing with its form in the state, and not without it.
- When the decision model's context is known, the page in `state` is cut, at a line, so the whole question fits it.

| Answer | Run |
|---|---|
| `done` (reached) | ends as `finished`, or `unsure` under 0.5 confidence |
| `impossible` | ends as `cannot_reach` |
| `click:eN` / `open:eN` / `type:eN` | that action is sent to the page, after the commit check for a `click` |
| `commits` (commit check) | ends as `stopped_before_submit`, the click not made |
| any other label | refused by `parse_choice_response`; ends as `failed` |

There is no free text anywhere in a decision. Usage is recorded on the `page_decision` trace span.
