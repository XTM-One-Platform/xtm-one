# Contract: `act_on_page` Tool

The agent sees this tool only on a turn with `supports_page_actions` (FR-005). Its `source` is `"builtin"`.

## Input

```json
{
  "type": "object",
  "properties": {
    "goal": {
      "type": "string",
      "description": "One destination or action on the page, in one sentence, naming what to click by the labels the page shows, through the menus that lead to it when you know them (e.g. 'Open Analyses > Reports' rather than 'open the reports list'): the step chooser reads only the page. For several destinations, call the tool once per destination."
    },
    "text": {
      "type": "string",
      "description": "A value to type into a field of the page (a search term, a name), when the goal needs one."
    },
    "path": {
      "type": "string",
      "description": "A path within the host application to open first, when it is known."
    },
    "submit": {
      "type": "boolean",
      "description": "True only when the user asked to save, create, send or delete what the page holds. Otherwise the run stops before any click that does."
    }
  },
  "required": ["goal"]
}
```

`text` is at most 1000 characters. `path` follows FR-013: it starts with `/`, not `//`, and has no scheme. An invalid one is refused before any frame is sent. `submit` defaults to false (FR-014).

## Result

The result is a JSON string. Fields the platform computed stay outside the quarantine block. Everything the page wrote is inside one `wrap_untrusted(source="host page")` block (research R7).

```text
{"outcome": "finished", "steps": 3, "truncated": false}
Untrusted external content follows. …
<<UNTRUSTED-…>>
[source: host page]
{"url": "/dashboard/analyses/reports/…", "title": "…",
 "steps": [{"action": "click", "target": "link \"Reports\"", "ok": true}, …],
 "withheld": "button \"Create\""}
<<END-UNTRUSTED-…>>
```

| `outcome` | Meaning | `ToolResult.is_error` |
|---|---|---|
| `finished` | the decision model judged the goal reached, with a confidence of at least 0.5 | false |
| `unsure` | the decision model judged the goal reached with a confidence under 0.5: it may be | false |
| `stopped_before_submit` | without `submit`, the next action (`withheld`: a click, an `open`, or typing that presses Enter) would save, create, send or delete, or the decision model judged it only moves through the page with a confidence under 0.5, and was not made | false |
| `cannot_reach` | the decision model answered `impossible`, or it repeated an action the page did not perform (R6) | false |
| `unverified` | it repeated an action the page performed, on a snapshot that action left unchanged | false |
| `budget_reached` | 8 steps were used or 240 s passed | false |
| `failed` | a page wait timed out, the decision model errored, it answered a key it was not offered, or another run already holds the conversation's page | true |

One run holds a conversation's page at a time (a Redis claim, across replicas): a second call made while one runs is refused at once rather than replacing its pending action. A step weighs at most 200 of the page's actions and reports `truncated` beyond, and the run deadline is also kept between the questions of a step.

When the outcome is `failed` because of the model, the result names the decision model and its provider, so the agent can say which one is down (spec, edge cases).
