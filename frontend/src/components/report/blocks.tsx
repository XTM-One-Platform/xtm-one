/*
Copyright (c) 2025-2026 Filigran SAS

This file is part of the XTM One Enterprise Edition ("EE") and is
licensed under the XTM One Enterprise Edition License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
*/

/**
 * Block components for agent-generated reports.
 *
 * One component per entry in the backend's `REPORT_BLOCKS` catalog
 * (`backend/app/services/report_spec.py`). The agent picks a block and fills its
 * fixed prop set — it never controls the markup — so a whole executive document
 * can be rendered from structured data without giving away the page.
 *
 * `rich_text` is the single exception and always goes through `sanitizeRichText`
 * (the DOMPurify strict profile shared with decks) before it is inserted.
 *
 * Styling lives in `report.css`; classes are plain CSS (not Tailwind utilities)
 * because the report is a self-contained visual surface with its own `--report-*`
 * token scale and its own print stylesheet.
 */
import { Fragment, type ReactNode } from "react";

import { sanitizeRichText } from "@/components/presentation/sanitize";
import { useFormatter } from "@/i18n";
import type {
  ReportAnnexBlock,
  ReportAssessmentTableBlock,
  ReportBlock,
  ReportBlockTable,
  ReportCalloutBlock,
  ReportChainBlock,
  ReportChecklistBlock,
  ReportDecisionsBlock,
  ReportFillInBoxBlock,
  ReportKpiRowBlock,
  ReportMatrixBlock,
  ReportProseBlock,
  ReportRiskTableBlock,
  ReportScenariosBlock,
  ReportSourcesBlock,
  ReportTimelineBlock,
} from "@/types";

/** Render a sanitized `rich_text` fragment, or nothing when it is empty. */
export function RichText({
  html,
  className = "report-rich-text",
}: {
  html?: string;
  className?: string;
}) {
  const clean = sanitizeRichText(html);
  if (!clean) return null;
  // Safe by construction: `sanitizeRichText` is the DOMPurify strict profile
  // (no script/iframe/form/style, no event handlers, http(s)/mailto links only).
  return <div className={className} dangerouslySetInnerHTML={{ __html: clean }} />;
}

/**
 * Section heading, rendered only when the block carries one.
 *
 * The anchor `id` is what the derived table of contents links to; it comes from
 * `ReportView`, which owns the numbering of blocks, so two blocks can never
 * compute the same id independently.
 */
function BlockTitle({ block, anchorId }: { block: ReportBlock; anchorId?: string }) {
  if (!block.title) return null;
  return (
    <div className="report-section-head" id={anchorId}>
      {block.number ? <div className="report-secno">{block.number}</div> : null}
      <h2>{block.title}</h2>
    </div>
  );
}

function KpiRow({
  block,
  anchorId,
}: {
  block: ReportKpiRowBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-kpis" aria-label={block.title} id={anchorId}>
      {block.items.map((item, index) => (
        <article className="report-card report-kpi" key={index}>
          <div className="report-kpi-value">{item.value}</div>
          <div className="report-kpi-label">{item.label}</div>
          {item.note ? <div className="report-kpi-note">{item.note}</div> : null}
        </article>
      ))}
    </section>
  );
}

function Prose({ block, anchorId }: {
  block: ReportProseBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <RichText html={block.lead} className="report-rich-text report-lead" />
      {block.alert ? (
        <div className="report-alert">
          {block.alert.label ? (
            <span className="report-alert-label">{block.alert.label} </span>
          ) : null}
          <RichText html={block.alert.rich_text} />
        </div>
      ) : null}
      <RichText html={block.rich_text} />
    </section>
  );
}

function Decisions({ block, anchorId }: {
  block: ReportDecisionsBlock;
  anchorId?: string;
}) {
  const { t_i18n } = useFormatter();
  const CONFIDENCE_LABELS = {
    high: t_i18n("HIGH"),
    medium: t_i18n("MEDIUM"),
    low: t_i18n("LOW"),
  } as const;
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <div className="report-decisions">
        {block.items.map((item, index) => (
          <article className="report-decision" key={index}>
            <div className="report-decision-label">
              {/* Numbered for the author when they did not label it themselves —
                  a decision block reads as a list of asks, and "01 / 02 / 03" is
                  what makes it one. */}
              {item.label ??
                t_i18n("DECISION {n}", {
                  n: String(index + 1).padStart(2, "0"),
                })}
            </div>
            <h3>{item.title}</h3>
            {item.text ? <p>{item.text}</p> : null}
            <RichText html={item.rich_text} />
            {item.confidence || item.confidence_label ? (
              <div
                className="report-decision-confidence"
                data-confidence={item.confidence}
              >
                {item.confidence_label ??
                  (item.confidence ? CONFIDENCE_LABELS[item.confidence] : null)}
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function Chain({ block, anchorId }: {
  block: ReportChainBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <div className="report-chain">
        {block.steps.map((step, index) => (
          <div className="report-step" key={index}>
            <div className="report-step-num">{index + 1}</div>
            <span className="report-step-title">{step.title}</span>
            {step.text ? <p>{step.text}</p> : null}
          </div>
        ))}
      </div>
      {block.note ? <p className="report-note">{block.note}</p> : null}
    </section>
  );
}

function AssessmentTable({ block, anchorId }: {
  block: ReportAssessmentTableBlock;
  anchorId?: string;
}) {
  const { t_i18n } = useFormatter();
  const CONFIDENCE_LABELS = {
    high: t_i18n("HIGH"),
    medium: t_i18n("MEDIUM"),
    low: t_i18n("LOW"),
  } as const;
  // `nature` and `appraisal` are optional, and an always-present empty column
  // reads as missing data rather than as a column nobody filled. So the table
  // takes the shape of the rows it actually has.
  const hasNature = block.rows.some((row) => !!row.nature);
  const hasAppraisal = block.rows.some((row) => !!row.appraisal);
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <table className="report-table">
        <thead>
          <tr>
            {/* With an appraisal column present, the first column holds the claim
                and the third holds the judgement on it — heading both
                "Assessment" would leave the reader guessing which is which. */}
            <th>{hasAppraisal ? t_i18n("Element") : t_i18n("Assessment")}</th>
            {hasNature ? <th>{t_i18n("Nature")}</th> : null}
            {hasAppraisal ? <th>{t_i18n("CTI appraisal")}</th> : null}
            <th>{t_i18n("Confidence")}</th>
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, index) => (
            <tr key={index}>
              <td>{row.statement}</td>
              {hasNature ? (
                <td className="report-nature">{row.nature}</td>
              ) : null}
              {hasAppraisal ? <td>{row.appraisal}</td> : null}
              <td className="report-confidence" data-confidence={row.confidence}>
                {row.confidence_label ?? CONFIDENCE_LABELS[row.confidence]}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Matrix({
  block,
  anchorId,
}: {
  block: ReportMatrixBlock;
  anchorId?: string;
}) {
  const { t_i18n } = useFormatter();
  const LEVEL_LABELS = {
    critical: t_i18n("CRITICAL"),
    elevated: t_i18n("ELEVATED"),
    moderate: t_i18n("MODERATE"),
    controlled: t_i18n("CONTROLLED"),
  } as const;
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      {/* A matrix is the one block that can legitimately be wider than a phone.
          It scrolls inside its own container so the page body never does. */}
      <div className="report-matrix-scroll">
        <table className="report-matrix">
          <thead>
            <tr>
              <th className="report-matrix-axis">{block.axis_label}</th>
              {block.column_labels.map((label, index) => (
                <th className="report-matrix-axis" key={index} scope="col">
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {block.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                <th className="report-matrix-axis" scope="row">
                  {row.label}
                </th>
                {row.cells.map((cell, cellIndex) => (
                  <td
                    className="report-matrix-cell"
                    data-level={cell.level}
                    key={cellIndex}
                  >
                    {/* The level name is carried for screen readers and for print,
                        where the colour fills degrade to grey and the fill alone
                        would no longer distinguish one grade from another. */}
                    <span className="report-matrix-level">
                      {LEVEL_LABELS[cell.level]}
                    </span>
                    {cell.label}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {block.note ? <p className="report-note">{block.note}</p> : null}
    </section>
  );
}

function Scenarios({ block, anchorId }: {
  block: ReportScenariosBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <div className="report-scenarios">
        {block.items.map((item, index) => (
          <div className="report-scenario" key={index}>
            <h3>{item.title}</h3>
            {item.text ? <p>{item.text}</p> : null}
            {item.impact ? (
              <div className="report-scenario-impact">{item.impact}</div>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}

function RiskTable({ block, anchorId }: {
  block: ReportRiskTableBlock;
  anchorId?: string;
}) {
  const { t_i18n } = useFormatter();
  const LEVEL_LABELS = {
    critical: t_i18n("CRITICAL"),
    elevated: t_i18n("ELEVATED"),
    moderate: t_i18n("MODERATE"),
    controlled: t_i18n("CONTROLLED"),
  } as const;
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <table className="report-table">
        <thead>
          <tr>
            <th>{t_i18n("Situation")}</th>
            <th>{t_i18n("Level")}</th>
            <th>{t_i18n("Recommended posture")}</th>
          </tr>
        </thead>
        <tbody>
          {block.rows.map((row, index) => (
            <tr key={index}>
              <td>{row.situation}</td>
              <td>
                <span className="report-risk-badge" data-level={row.level}>
                  <i className="report-dot" aria-hidden="true" />
                  {row.level_label ?? LEVEL_LABELS[row.level]}
                </span>
              </td>
              <td>{row.posture}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Callout({ block, anchorId }: {
  block: ReportCalloutBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-callout report-section">
      <div>
        <BlockTitle block={block} anchorId={anchorId} />
        <RichText html={block.rich_text} />
      </div>
      {block.score ? (
        <div className="report-score">
          {block.score}
          {block.score_label ? (
            <small className="report-score-label">{block.score_label}</small>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function Timeline({ block, anchorId }: {
  block: ReportTimelineBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <div className="report-timeline">
        {block.phases.map((phase, index) => (
          // The two cells are siblings in one grid, not a wrapper per phase: the
          // rails line up across phases only if every cell is a direct child.
          <Fragment key={index}>
            <div className="report-phase-label">{phase.label}</div>
            <div className="report-phase-body">
              {phase.headline ? (
                <strong className="report-phase-headline">{phase.headline}</strong>
              ) : null}
              {phase.items?.length ? (
                <ul>
                  {phase.items.map((item, itemIndex) => (
                    <li key={itemIndex}>{item}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          </Fragment>
        ))}
      </div>
    </section>
  );
}

function Checklist({ block, anchorId }: {
  block: ReportChecklistBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-card report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <ul className="report-checklist" data-columns={block.columns ?? 2}>
        {block.items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

function FillInBox({
  block,
  anchorId,
}: {
  block: ReportFillInBoxBlock;
  anchorId?: string;
}) {
  return (
    <section className="report-fill-in report-section" id={anchorId}>
      {block.title ? <h3>{block.title}</h3> : null}
      {block.fields.map((field, index) => (
        <p key={index}>
          <strong>{field.label} : </strong>
          {field.value ? (
            <span>{field.value}</span>
          ) : (
            // No value means "the reader fills this in" — the rule is the point
            // of the block, so it is rendered rather than left blank.
            <span className="report-blank" />
          )}
        </p>
      ))}
      {block.notes?.length ? (
        <div className="report-status-strip">
          {block.notes.map((note, index) => (
            <span key={index}>{note}</span>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function SimpleTable({ table }: { table: ReportBlockTable }) {
  return (
    <table className="report-table">
      <thead>
        <tr>
          {table.headers.map((header, index) => (
            <th key={index}>{header}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {table.rows.map((row, rowIndex) => (
          <tr key={rowIndex}>
            {row.map((cell, cellIndex) => (
              <td key={cellIndex}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Annex({
  block,
  forceOpen,
  anchorId,
}: {
  block: ReportAnnexBlock;
  forceOpen?: boolean;
  anchorId?: string;
}) {
  return (
    <section className="report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <details className="report-details" open={forceOpen || block.open}>
        <summary>{block.summary}</summary>
        <div className="report-details-inside">
          {block.items?.length ? (
            <ul>
              {block.items.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          ) : null}
          <RichText html={block.rich_text} />
          {block.table ? <SimpleTable table={block.table} /> : null}
          {block.note ? <p className="report-note">{block.note}</p> : null}
        </div>
      </details>
    </section>
  );
}

function Sources({
  block,
  forceOpen,
  anchorId,
}: {
  block: ReportSourcesBlock;
  forceOpen?: boolean;
  anchorId?: string;
}) {
  const { t_i18n } = useFormatter();
  return (
    <section className="report-section">
      <BlockTitle block={block} anchorId={anchorId} />
      <details
        className="report-details"
        open={forceOpen || (block.open ?? true)}
      >
        <summary>{block.title ?? t_i18n("Sources and method")}</summary>
        <div className="report-details-inside report-sources">
          {block.intro ? <p>{block.intro}</p> : null}
          <ol>
            {block.items.map((item, index) => (
              <li key={index}>
                {item.ref ? (
                  <span className="report-source-ref">{item.ref}</span>
                ) : null}
                {item.url ? (
                  // `rel` is set here for the same reason the sanitizer forces it
                  // on rich-text links: a report renders inside the app shell.
                  <a href={item.url} target="_blank" rel="noopener noreferrer">
                    {item.label}
                  </a>
                ) : (
                  item.label
                )}
                {item.note ? <span className="report-note">, {item.note}</span> : null}
              </li>
            ))}
          </ol>
          <RichText html={block.limits} />
        </div>
      </details>
    </section>
  );
}

/**
 * Render one block by type.
 *
 * An unknown block (a report stored by a newer backend than this bundle) renders
 * nothing rather than throwing: a document missing one section it could not
 * understand is a far better failure mode than a blank page where the whole
 * report used to be. Decks make the same trade-off in `layouts.tsx`.
 */
export function ReportBlockBody({
  block,
  forceOpen,
  anchorId,
}: {
  block: ReportBlock;
  forceOpen?: boolean;
  anchorId?: string;
}): ReactNode {
  switch (block.block) {
    case "kpi_row":
      return <KpiRow block={block} anchorId={anchorId} />;
    case "prose":
      return <Prose block={block} anchorId={anchorId} />;
    case "decisions":
      return <Decisions block={block} anchorId={anchorId} />;
    case "chain":
      return <Chain block={block} anchorId={anchorId} />;
    case "assessment_table":
      return <AssessmentTable block={block} anchorId={anchorId} />;
    case "scenarios":
      return <Scenarios block={block} anchorId={anchorId} />;
    case "risk_table":
      return <RiskTable block={block} anchorId={anchorId} />;
    case "matrix":
      return <Matrix block={block} anchorId={anchorId} />;
    case "callout":
      return <Callout block={block} anchorId={anchorId} />;
    case "timeline":
      return <Timeline block={block} anchorId={anchorId} />;
    case "checklist":
      return <Checklist block={block} anchorId={anchorId} />;
    case "fill_in_box":
      return <FillInBox block={block} anchorId={anchorId} />;
    case "annex":
      return <Annex block={block} forceOpen={forceOpen} anchorId={anchorId} />;
    case "sources":
      return <Sources block={block} forceOpen={forceOpen} anchorId={anchorId} />;
    default:
      return null;
  }
}
