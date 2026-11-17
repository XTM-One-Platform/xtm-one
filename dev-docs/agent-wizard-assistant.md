# Agent wizard assistant (experiment, `AGENT_WIZARD_ASSIST`)

A prompt bar under the agent-creation wizard (`/agents/new/wizard`): the person
describes the agent, or a change to it, and watches the wizard fill in step by
step, each field it changes pointed at.

It is an experiment ahead of the drive-UI epic, which will give every
page one way to be driven by an agent. Until then this mechanism is the
wizard's own: nothing outside the wizard depends on it, and the epic replaces
it rather than building on it.

## Contract

- `POST /api/v1/ai/agent-wizard/assist` (`backend/app/api/v1/ai.py`): the
  request, the draft as the person has it, whether the wizard has started,
  the conversation so far and the icon names the picker renders. Behind the
  flag (403 off, before authentication) and gated like the agent it prepares
  (`require_agentic_ai`). Nothing is stored.
- The answer is a reply and a **patch**: only the fields the assistant
  changes, each list whole (`WizardPatch`,
  `backend/app/services/agent_wizard_assistant.py`), plus `focus_step`, the
  step the wizard lands on. A template is chosen only while the wizard is
  still on its gallery.
- What the model is offered is what the wizard offers the caller
  (`wizard_offer`): the node catalog its pickers read (`catalog_sources`), the
  connected integrations of the integrations page's library set, the enabled
  templates. Every id of the patch is checked against that offer; a refused
  patch goes back to the model once with the reasons, then what is still
  refused is left out and listed in `notes`, never played.
- Names other people chose reach the model inside JSON; the draft's ids are
  named from the caller's offer only, an id it does not list is shown as
  unavailable.

## Playing the patch

`frontend/src/pages/new-agent-wizard/assist-plan.ts` turns the patch into
operations in the wizard's own step order (open a step, type a text, tick or
untick one item, set a value); `useAssistPlayback.ts` plays them one at a time
onto the draft as it is when each plays, so what the person types meanwhile is
kept. `AssistFrame` rings the field being changed and the canvas badges the
node. Reduced motion applies everything at once. Stop drops the answer in
flight or the rest of the playback; Undo puts back the wizard as it was when
the request was sent, back to the gallery when the answer opened it.
