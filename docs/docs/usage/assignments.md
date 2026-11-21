# Assignments and triggers

An assignment is a job an agent does on its own: a prompt that tells the agent what to do, and a trigger that says when to do it. A daily briefing, a reaction to a new email, a review of every finished simulation: each is one assignment on one agent, and every run goes through the agent's normal reasoning with its tools and knowledge.

## Create an assignment

Open the agent and find the `Assignments` card on its `Overview` tab. It lists the agent's assignments, how many of them are active, and a pause switch on each. `Add Assignment` opens the `New Assignment` form:

1. Type an `Assignment name`.
2. Pick the trigger type (see [Choose a trigger](#choose-a-trigger)) and fill in what it asks for: a schedule, an integration and its event, or a platform event.
3. Write the `Action prompt`: what the agent should do when the assignment runs.
4. Click `Create Assignment`.

`Add Assignment` is shown to the people who can manage the agent. On an agent shared with other people, the platform first shows what the new assignment lends to them and asks you to confirm.

An assignment is created switched on: its trigger starts runs as soon as it is saved. Assignments that come with an agent template are created switched on too, while an imported agent's, a duplicated agent's or a catalog install's start switched off, waiting for your review.

!!! tip "Try it before you schedule it"
    `Run Now`, at the top of the assignment page, starts a run immediately, whatever the trigger. Run it once by hand and read the result before you let a schedule or an event start it.

## Choose a trigger

| Trigger | When the assignment runs |
| --- | --- |
| `Scheduled` | On a schedule: pick a preset such as `Every 5 minutes`, or a `Custom cron` expression. |
| `Webhook` | When another system posts to the assignment's own URL. The URL is generated once the assignment exists. See [Webhook triggers](webhook-triggers.md). |
| `Integration event` | When something happens in a service the agent is bound to: a new email, a Teams message, a GitHub event, an OpenCTI entity created, an OpenAEV simulation finished. The agent must have an integration with event support bound in its `Configuration`; otherwise the form says so. |
| `Manual` | Only when someone starts it with `Run Now`, or when an agent starts it. |
| `Work item` | Makes the assignment the agent's inbox: it picks up the open work items the agent owns and runs once per item. See [Work an agent's queue](agents.md#work-an-agents-queue-the-work-item-trigger). |
| `Platform event` | When something happens inside XTM One: a work item changes, a run fails, a background task ends, a timer the agent armed comes due. See [React to what happens in the platform](agents.md#react-to-what-happens-in-the-platform). |

A `Manual` assignment can ask for inputs. On its `Trigger` card, `Input Parameters` defines the fields (`Key`, `Label`, `Default value`, `Required` or `Optional`); `Run Now` then opens a form for them, and the agent receives the values in its prompt.

Every trigger can wait before it runs: `Delay before execution` takes a duration such as `10m` or `1h`. Leave it empty to run at once.

## The assignment page

Click an assignment to open its page. The header shows its trigger type, its delay, `Dry run` when every tool call is only simulated, and its number of runs. The header actions are `Run Now`, `View Traces` (every run of the assignment in the Trace Explorer), the pause switch, and `Delete assignment`.

The page has four tabs:

- `Overview`: the `Total Runs`, the `Status`, `Last Triggered` and `Recent Health` tiles; the `Run As` card for a company-managed assignment (see [Whose identity a run uses](#whose-identity-a-run-uses)); the `Trigger` card, where you change the trigger; and the `Parameters` card.
- `Prompt & Bindings`: the prompts, and what the assignment may use of its agent (see [Prompts and gates](#prompts-and-gates) and [Narrow what the assignment uses](#narrow-what-the-assignment-uses)).
- `Channels & Bots`: which messaging channels the assignment may receive from and publish to, within what its agent already allows. By default it inherits the agent's configuration. See [Channels and bots](../administration/channels/index.md).
- `Activity`: the assignment's runs.

The `Parameters` card holds the run settings: `Execution timeout`, `Max iterations`, `Retry on failure` with its `Max retries`, `Human in the loop` (let the agent pause and ask you when it is unsure), `Dry run`, the knowledge bases the assignment reads and writes, the runners it may drive for computer use (when runners are enabled), `AI-generated label` on outgoing messages, a bell `Notification` when a run completes, and whether its results appear in the dashboard digest. A setting left on `Inherit` follows the agent.

When `Human in the loop` is on, a paused run shows its question, what the agent found and, when it gives them, numbered options with the one it recommends and links to its sources. Answer with an option's letter or name to choose it, or write your own answer.

## Prompts and gates

The `Prompt & Bindings` tab holds up to three steps, run in this order:

1. `Tool Check` (optional): the platform calls one read-only tool of the agent, with no AI model, and runs the assignment only when the result passes (`Runs when the result is not empty`, `Runs when the result is empty`, or `Runs when a field matches`). A run whose check fails is skipped with no tokens spent.
2. `Gate Prompt` (optional): one short AI call that answers yes or no on the event before the full run starts.
3. `Action Prompt`: what the agent does.

Integration-event triggers can also drop events before any of this, with event filters on the `Trigger` card. How filters, the tool check and the gate prompt work, with examples, is in [When an assignment runs: gates and event filters](agents.md#when-an-assignment-runs-gates-and-event-filters).

## Narrow what the assignment uses

By default an assignment uses everything its agent has. The `Prompt & Bindings` tab can narrow it for this assignment only, never widen it:

- `Integration Bindings`: keep all of the agent's integrations and tools, or choose `Custom scope` and select some.
- The elements an integration works on (channels, repositories and the like): a subset of those selected on the agent.
- `Built-in Tool Scope`: a subset of the agent's built-in functions.

## Whose identity a run uses

A run of an assignment you own works as you: with your connected integrations, your model preference and your memory.

A company-managed assignment shows a `Run As` card. Its default is `Default: run as owner`. An administrator can pick `A specific user`, whose identity scheduled, webhook and integration-event runs then use. `Run for all users` turns each scheduled run into one run per active user, each limited to that user's own access, and `Hide runs from other users` keeps each run private to the user it ran as. Manual runs always run as the person who starts them.

## Pause and resume

The pause switch, on the assignment's row in the agent's `Assignments` card and in the header of its page, stops its trigger from starting runs. `Resume` switches it back on. The `Status` tile and the badge of a paused assignment say why it is paused:

| Reason | What happened |
| --- | --- |
| `paused by a user` | Someone switched it off. The banner says who and when. |
| `paused by an agent` | An agent switched it off with its builder tools. |
| `auto-paused: integration auth failing` | Its scheduled runs kept failing to authenticate to an integration. Fix the integration's credentials, then resume. |
| `auto-paused: integration resource missing` | Its scheduled runs kept failing on a resource its integration no longer sees. See [When an integration resource goes missing](work-history.md#when-an-integration-resource-goes-missing). |
| `auto-paused: invalid schedule` | Its schedule could not be read. Correct the cron expression on the `Trigger` card, then resume. |
| `created off, awaiting review` | It came from an import, a duplicate or a catalog install, which start switched off. |
| `switched off by the deployment config` | The deployment defines this built-in assignment as switched off. |
| `stopped by an emergency stop` | Shown as `Emergency stop`. It comes back when the stop is lifted, not one at a time. See [Emergency stop](../administration/emergency-stop.md). |

An assignment whose owner's account is deactivated shows `paused: owner's account deactivated` and holds every trigger, `Run Now` included, until the account is back. See [When an assignment's owner is deactivated](work-history.md#when-an-assignments-owner-is-deactivated).

## Triggers XTM One sets up in OpenCTI and OpenAEV

An `Integration event` assignment on OpenCTI or OpenAEV needs the other platform to call XTM One. XTM One can create what that takes on the other platform's side, a webhook notifier and a live trigger, under the integration's own account, then check it every 15 minutes, repair it, and remove it when the assignment is switched off or deleted.

Two things are needed:

- An administrator sets the webhook secret on the registered OpenCTI or OpenAEV integration, in its `Configuration` card. The field shows `Stored` once set.
- The assignment's trigger asks for the setup. The assignment page does not offer this yet: an assignment created by an agent with the agent builder tools, or through the API, can.

The events, filters and rules are described in [React to OpenCTI events](xtm-suite/opencti.md#react-to-opencti-events) and [React to OpenAEV events](xtm-suite/openaev.md#react-to-openaev-events).

## Where runs appear

Every run is listed on the assignment's `Activity` tab, and with every other agent run under [`Operations`](work-history.md), where you open a run's detail, review its result, answer a question it asked, and rate it. A run that ends with a notification also reaches your notification bell.

You can also create, change, pause and run assignments from a conversation with an assistant that has the agent builder tools (see [Building agents from a conversation](agents.md#building-agents-from-a-conversation)).

## Next step

Follow what your assignments did in [`Operations`](work-history.md), or wire another system to an assignment with [Webhook triggers](webhook-triggers.md).
