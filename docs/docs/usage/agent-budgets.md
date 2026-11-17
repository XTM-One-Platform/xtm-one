# Run budgets

A run budget caps what **one run** costs. A run is a single piece of
agent work: one chat turn, or one assignment execution. The budget bounds
it along four dimensions at once —
money, tokens, tool calls and wall-clock time — and stops the run when
any of them is reached.

Budgets exist because the natural limits are the wrong shape. `Max
iterations` counts tool-calling rounds, and a single round can fan out
into a dozen parallel tool calls. A run still making progress also goes
past it, up to 1000 rounds (see [Long runs](../deployment/configuration.md#long-runs)).
A model change can multiply the cost of the same work without changing
anything you configured. A budget measures what you actually care about
instead: it is what bounds a long run.

## Where a budget is configured

There are two places, from broadest to narrowest.

**The platform**, set by an administrator in the deployment's
environment. This is the default every run inherits and, just as
importantly, the ceiling nobody else can raise. See
[Agent run budget](../deployment/configuration.md#agent-run-budget)
for the settings themselves.

**One agent**, on its `Value` tab, below the agent's ROI. This is where you say "this research
agent may spend more than the platform default" or "this triage bot must
never spend more than a few cents". It applies wherever that agent runs.

## The agent's run budget

Open an agent and choose `Value` (called `Budget` before; old links still
open it): the run budget sits under the agent's ROI. Each of the four limits is a single
field, and **an empty field means "inherit", not "zero"**. The
placeholder tells you what it would inherit, so you can see the effective
budget without saving anything.

That distinction is the whole point of leaving fields empty. A budget
that only pins the cost keeps tracking the platform's token, tool-call
and time limits — including any later change an administrator makes to
them. Typing the inherited numbers in to "be explicit" freezes a copy
that silently stops following the platform.

Use the `Inherit` link next to a field to hand a dimension back.

### Who may raise a limit

Anyone who can edit an agent may **lower** any limit. Spending less is
never a privilege.

**Raising a limit above the platform budget requires an administrator.**
That is what the platform setting is for: it is the budget a user cannot
exceed on their own. If you are not an admin, the tab says so up front,
and a field you push above the ceiling is flagged with the platform's
value before you attempt to save — you will not discover it as a rejected
save.

If your agent genuinely needs a larger budget than the platform allows,
ask an administrator to set it. Once they have, the value stays: you can
keep editing everything else about the agent without the budget being
questioned again, and without being able to raise it further yourself.

!!! note "Three settings that need no admin"
    The final-answer reserve, the cheaper-model failsafe and the choice
    of fallback model are not gated, because none of them can increase
    what a run spends. The reserve only moves the in-run limit *down*,
    and the failsafe keeps the same cost ceiling — it just buys more work
    under it.

### The final-answer reserve

A run that spends its entire budget on work has nothing left to pay for
the call that turns that work into an answer, so it would return a raw
digest of tool output instead of a written result.

The reserve prevents that: a slice of the cost and token ceilings is held
back for the closing answer. Work is gated at the remainder; the final
answer may spend the full budget. The platform default is 10%, which
comfortably covers one closing call.

## Finishing on a cheaper model instead of stopping

A cost ceiling of, say, $0.50 on an expensive model usually means "don't
spend more than $0.50 *at this quality*" — not "abandon the task". The
same $0.50 buys several times more work on a cheaper model.

Turn on `Finish on a cheaper model` and a run that reaches its **cost**
ceiling switches to a cheaper model and carries on instead of stopping.
The rest of the run, including its closing answer, is charged at that
model's rates, so the ceiling still holds. The agent is told the switch
happened, and the run's trace records it, so cost attribution stays
readable per call.

Deliberately bounded:

- **Once per run.** Never a ladder that limps along on progressively
  worse models.
- **Cost only.** Tokens, tool calls and time are model-independent — a
  cheaper model buys no extra wall clock.

`Fallback model` chooses which model that is. Left on `Platform fallback
model`, it uses the one configured in `Settings` > `AI Models` >
`Fallback Model` — the same model the platform already falls back to when
a provider fails, so there is one cheaper model to reason about rather
than two.

The switch is refused — and the run stops at its ceiling instead — when it
could not be made safely: when no pricing is known for the fallback (with
no price there is no way to know the next call is actually cheaper), when
the fallback's context window cannot hold the agent's system prompt, or
when switching would silently drop a structured-output guarantee the run
depends on. In each case stopping is the honest outcome; failing mid-run
is not.

## How the layers resolve

Nearest wins, **dimension by dimension**. For any agent step, each limit
is taken from the first layer that pins it:

1. the agent's own run budget, on its `Value` tab,
2. the platform.

Per-dimension is what makes partial budgets safe. An agent budget that pins only
the cost does not unbound the token limit — the platform's still applies.
Worked through:

| | Cost | Tokens |
|---|---|---|
| Agent's run budget | $2.00 | *(not set)* |
| Platform | $5.00 | 1,000,000 |
| **Effective** | **$2.00** | **1,000,000** |

## A budget you did not type yourself

[Who may raise a limit](#who-may-raise-a-limit) governs every way a budget
can reach your account, not just a value you type into the tab. Which of
two things happens depends on whether you chose the number:

- you typed it — editing an agent: the save is refused, naming the
  dimension and the platform's value, so you know exactly what to change;
- you copied it — duplicating an agent, importing one, restoring an older
  version: the action goes through and any limit above the platform
  ceiling arrives **lowered to that ceiling**. Blocking a duplicate over a
  number somebody else typed would get in the way of the ordinary "copy
  the company agent and adapt it" workflow, and lowering it prevents the
  same escalation.

Either way you cannot end up holding a budget an administrator did not
grant. An admin's copy keeps its values, since they could have typed them.

## What happens when a budget runs out

The run stops making model calls and writes its final answer from the
work it completed, using the reserve. The answer says which limit was
reached, so the outcome is a shorter result rather than a failure with
nothing to show.

Time is the exception in one respect: the duration limit is folded into
the run's existing deadline, and whichever bound is tighter applies.

## When a budget does not apply

**Cost needs pricing.** The cost ceiling is enforced from the platform's
model price table, which is seeded for known models. If no price is known
for the model a run uses, cost is **not** enforced for that run — a
warning is logged and the token, tool-call and time limits still apply.
Missing pricing data must never be able to stop every run on the
platform.

**Platform limits bound autonomous work.** The platform's own settings
apply to assignment runs. A budget you configure on an agent applies
wherever that agent runs, chat included — configuring a budget is an
explicit act, so it takes effect everywhere.

!!! warning "A budget smaller than one call"
    A model call the remaining budget cannot cover at the model's full
    maximum output is made with a lower maximum output, sized to what is
    left. A limit below the cost or tokens of a single call even with its
    maximum output lowered to 1,024 tokens makes every run stop immediately
    and return a summary of tool output. The platform logs an explicit
    warning at the start of such a run. Raise the budget.

## Next step

Read [Agentic flow](agentic-flow.md) for how agents hand work to each other,
or [Agents](agents.md) for the other tabs on an agent's detail page.
