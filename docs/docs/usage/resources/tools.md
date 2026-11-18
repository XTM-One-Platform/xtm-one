# Tools

Tools are the actions agents can take directly. This page gives you a quick tour of the `Tools` screens.

!!! note "Custom tools are off by default"
    In XTM One, custom tools are switched off by default, and the `Tools` page shows the built-in tools only. An administrator turns them on by switching off `Disable custom tools` in `Settings > Security`, or with its environment variable (see [Settings > Security options](../../deployment/configuration.md#settings-security-options)).

## The list page

In the left navigation, open `Tools & Integrations > Tools`. The page opens on the `Built-in Tools` tab: platform-provided capabilities such as Files, Web, Design, Threat Intelligence, Coordination, and Platform. A capability this deployment has switched off, such as the code interpreter, is not listed. So this page is often less about configuration and more about understanding what the platform already makes available to agents. Each card tells you whether the tool is a standard platform capability, whether it is active, and what category of action it adds.

The `Browser` tool browses without a sign-in by default. Give an agent [browser profiles](browser-profiles.md) and it opens those applications signed in, without seeing the credentials. To review a page as a person sees it, the agent can set the window size (from 320 x 240 to 3840 x 2160; each session opens at 1280 x 720), hover an element to see its tooltip or hover state (the pointer goes to a point of the element itself, so a thin curved line such as a graph edge is reached even when the middle of its box is covered by a layer beneath), drag an element by a given distance with real pointer input (a resize handle or a slider follows it as it would a person's mouse), capture one element instead of the whole window, and read the messages and errors the page logged to its console since it was opened. A script the agent runs on a page stops after 30 seconds (the agent may give it up to 2 minutes), so a page busy with a long task never holds the conversation. When the agent's browser session is closed for being idle or its browser stops, the next session gets back the cookies and site storage the last one had - a site it signed in to stays signed in - and the agent is told the session was re-created. Closing the session on purpose signs it out.

Each card is one capability. Several former cards now share one: `Files` covers file generation, reading and editing, HTML rendering and image generation; `Web` covers reading web pages and the browser; `Design` covers template search, template authoring and reading the design of a source document; `Threat Intelligence` covers the STIX bundle validator, the grounded STIX extraction (`extract_stix_from_document`, which turns a report into a STIX 2.1 bundle on the agent's own model) and the FIMI analytics; `Coordination` covers work items, shared state and worksets; `Platform` covers managing the platform from chat (agents, assignments, knowledge bases and the other resources) and fleets. An agent configured before the change keeps exactly the functions it had: its former tools are shown under the new card, and an agent can still be configured through the API with a former tool name, listed as deprecated.

When an administrator has turned custom tools on, a second tab, `Custom Tools`, lists them. It behaves like the other resource libraries, with search, visibility and tag filters, and an `Add Tool` action. A third tab, `Repositories`, syncs custom tools from a GitHub or GitLab repository (see [Sync custom tools from a repository](#sync-custom-tools-from-a-repository)).

The `Platform` tool acts as the person the agent works for, and offers only what that person may do: a function only an administrator may run (cancelling runs, reading and starting evaluations) is not offered to anyone else, and every other change stays limited to the resources the person may manage.

The General Assistant and the CTEM Assistant start with the same tools: Platform, Files, Presentations, Reports, Design, Web, Threat Intelligence and, for administrators, Platform Insights. Video Studio and the code interpreter are part of that set too, but both are off by default: Video Studio is an off-by-default feature (see [Feature flags](../../deployment/configuration.md#feature-flags)), and the code interpreter is off in XTM One until an administrator turns it on (see [Settings > Security options](../../deployment/configuration.md#settings-security-options)). An administrator can remove any of these tools in `Settings > General Assistant`.

The `Computer Use (Runners)` tool reads, writes and lists files on an enrolled machine with `runner_read_file`, `runner_write_file` and `runner_list_dir`. Their former names `read_file`, `write_file` and `list_dir` are still accepted, so an agent or an allow list that names them keeps working.

## Built-in tool detail pages

Opening a built-in tool leads to a detail page with `Overview`, `Configuration`, and `Activity` tabs.

- `Overview` shows what the tool does and whether it is used in real workflows — how many agents use it, how many runs it has, and (for Files, which includes image generation) the current image model.
- `Configuration` is usually informational. For Files it exposes the platform-wide image model: admins can change it, non-admins can only see it — so the experience differs by role even though the page is visible to everyone.
- `Activity` shows runs linked to agents using the tool.

## Custom tool detail pages

When an administrator has turned custom tools on, each custom tool has a detail page with `Overview`, `Configuration`, `Tests` and `Activity` tabs, with `Test Tool`, `Run tests` and delete actions in the header.

- `Overview` summarizes the tool: what it does, its language and timeout, its input parameters, and which agents use it.
- `Configuration` is where you define and edit the tool. The parts that matter are durable:
    - parameters define what the tool expects
    - code (JavaScript or Python) defines what the tool does
    - a timeout defines how long the platform will wait
    - visibility and sharing settings define who can discover and reuse it

Next to the code field, `Generate with AI` writes the code from a short description of what it should do. It reads the form as it stands: the language, the tool's name and description, and the parameters you declared. When the field already holds code, the button reads `Improve with AI` and reworks that code instead of starting over. The result lands in the editor for you to review; nothing is saved until you save the tool. The same button is offered in a flow, when you choose `Write a new script` on a script node.

`Test Tool` opens a dedicated test dialog — the fastest way to validate a custom tool before binding it to an agent. Its `Dry run` switch runs the tool with `dry_run` set to true (see [Dry run](#dry-run)).

## Tests and dry run

A custom tool can declare its own tests, in its `Tests` tab, in two forms you can combine:

- **Data cases**: a name, the inputs, whether the case is a dry run, and what the output must look like (`equals`, `contains`, `not_contains`, `regex`, `json_subset`), or that the tool must fail (`expect_error`, `error_contains`). Each case is one run of the tool, exactly as an agent runs it.
- **Test code**, in the tool's language. In Python, `test_*` functions or `unittest` classes call `run_tool(inputs, dry_run=True)`; in JavaScript, `test(name, fn)` with `assert` (Node's `assert`) calls `await runTool(inputs, { dryRun: true })`. Both return what the tool printed and raise `ToolError` when the tool fails. Only the language's standard library is available.

`Run tests` in the header, or `Run all tests` in the tab, runs everything and shows each result with its output; the badge in the header keeps the outcome of the last full run. `Dry run only` skips the cases that are not dry runs and runs the test code with `dry_run` forced to true. Each data case also has its own button. Tests run in the same sandbox as the tool, with your variables.

### Dry run

Every run of a custom tool receives a `dry_run` variable (`$dry_run` in JavaScript): false when an agent calls the tool, true for a dry-run case or the `Dry run` switch of `Test Tool`. A tool that changes something (creates a ticket, blocks an address, sends a message) should check it and only describe what it would do. The platform cannot do it for the tool: the sandbox lets the tool reach the network, because a tool needs it to read too.

## Sync custom tools from a repository

The `Repositories` tab of the `Tools` page syncs custom tools from a GitHub or GitLab repository. `Add a repository` asks for the repository (`owner/repository`, `group/project`, or its address), a branch, tag or commit (the default branch when empty), a sub-directory, a credential and how often to sync. A public repository on github.com or gitlab.com needs no credential; a private one, or a self-managed GitLab, is read with a connected GitHub or GitLab integration you can use.

Each directory holding a `tool.yaml` becomes one custom tool:

```text
tools/whois-lookup/
  tool.yaml        # name, description, input_schema, timeout, tags, tests
  main.py          # the code (main.py, main.js or index.js, or the file named by "entry")
  test_main.py     # optional test code (test_main.py or main.test.js, or "test_file")
```

```yaml
name: Whois lookup
description: Looks a domain up in WHOIS
timeout: 60
tags: [osint]
input_schema:
  type: object
  properties:
    domain: { type: string, description: The domain }
  required: [domain]
tests:
  - name: dry run
    inputs: { domain: example.com }
    dry_run: true
    expect: { contains: [example.com] }
```

Each sync reads the commit the branch points at, then:

- a tool whose files did not change is left as it is;
- a new or changed tool runs its tests, and the new version is activated only when they pass. A tool that declares no tests is activated too, and the report says it is untested. When the tests fail, the active version stays and the report shows the failures; a new tool is not created;
- a tool removed from the repository is disabled, not deleted, because agents may use it. When its directory comes back, the sync enables it again (once its tests pass, if it changed); a tool you disabled yourself stays disabled;
- a tool that cannot be read (an invalid `tool.yaml`, a missing file) is reported and left as it is.

`Sync now` syncs at once, unless a sync of the same repository is already running, and `Last sync report` shows what the last sync did, tool by tool. Every sync that changes a tool is recorded in the audit log.

A synced tool is read-only on its page: its name, code, parameters and tests change in the repository. You can still share it, enable or disable it, and hide it from chat. `Detach` turns it into an ordinary tool you can edit; the repository no longer updates it (`Sync it again` in the report undoes that for the next sync). Deleting a repository keeps its tools, detached, unless you choose to delete them too. A repository you add is yours; an administrator can add a company-managed one, whose tools are company-managed.

## Waiting is a tool too

Some capabilities are not on the `Tools` page because every agent has them. `wait` is one: it lets an agent pause on purpose — typically after handing a long task to another agent — and re-check when the work is likely done, instead of asking for a status update over and over.

You will see it in the chat as a countdown with the agent's own explanation of what it is waiting for, next to a `Continue now` link. Under it, each task or run the agent is waiting for is listed with its state (`Pending`, `Running` with how long it has been running, `Completed`, `Failed`...), updated as it changes:

- `Continue now` ends the pause and lets the agent carry on with whatever it has.
- The usual stop button cancels the whole response, pause included.

The agent also wakes up on its own as soon as the work it named has finished, so a five-minute wait often costs far less. That work can be a task it handed to another agent, or an assignment or flow run it started, for example a test run of an assignment. Admins set the ceilings in `Settings > General Assistant`.

An assignment run the agent starts from the chat (`trigger_assignment`) reports back to that conversation when it ends: if the agent was not waiting for it, the run's outcome - its status, how long it took and its result - resumes the conversation, and the agent tells you what came out. A run you cancel is not reported. When the agent asks to start an assignment that is already running, it is told which run is in progress, so it can wait for that one instead.

## Read-only expectations

Built-in tools are part of the platform: visible and understandable, but not treated like personal resources. The main experience is usually inspection and understanding rather than ownership and creation.

## Good habits

- Use the tool description to understand capability before attaching it to an agent.
- Check `Agents Using` before changing shared tool settings.
- Treat the image model of the `Files` tool as platform-wide, not per-agent.
- Expect built-in tools to be the main visible part of this page, unless custom tools are turned on.
