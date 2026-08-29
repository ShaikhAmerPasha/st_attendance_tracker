<!-- Implementation

Guidelines for writing good code for a developer:

    Choose clean code over clever code.
    Write object oriented code as much as possible.
    Keep function sizes small, ideally 10 lines.
    Try and keep files between 100 and 300 lines.
    Don't keep too many files in a folder or module. Try and keep it under 15.
    Avoid abbreviations.
    Use standard API as much as possible.
    Reuse. Write as little code as possible.
    Use Frappe UI, espresso design system for UI styling.
    Always write tests, and make sure they work.
    Build the minimum working app, then iterate towards your goals.
    Keep the verbosity less in new changes (inline comments, docstrings erc). Explain only what's absolutely needed in inline comments. Actual changes explanation can be part of commit message.

DocType

    Use column breaks and tab breaks to create user friendly doctype forms

Skills

Always load frappe-app-dev and frappe-ui skills before any implementation
Planning

For creating specs use tracer bullet approach.

    Tracer bullets comes from the Pragmatic Programmer. When building systems, you want to write code that gets you feedback as quickly as possible. Tracer bullets are small slices of functionality that go through all layers of the system, allowing you to test and validate your approach early. This helps in identifying potential issues and ensures that the overall architecture is sound before investing significant time in development.

Create specs in specs/. Maintain a PROGRESS.md file to track progress of implementation phases.


Testing

Use agent-browser for quick manual e2e checks.

Automated Playwright e2e (root package.json, specs in e2e/, CI via .github/workflows/ui-tests.yml) is planned but not yet implemented — none of that exists in the repo yet. Until it's built, rely on the automated test suite (bench --site excel run-tests --app st_attendance_tracker) plus manual agent-browser checks.
Credentials

The site is excel.localhost:8000 (Administrator/admin) -->


# CLAUDE.md

## Project

ST Attendance Tracker is a Frappe / ERPNext v15 app for attendance, daily tasks, recurring tasks, employee history, team dashboards, management reporting, and scheduled reminders/reports.

* App: `st_attendance_tracker`
* Frappe: `>=15,<16`
* Python: `>=3.10`
* Frontend: Frappe website pages, Jinja/HTML, JavaScript, CSS
* Development site: `excel.localhost:8000`

Treat this as a **Frappe application first**. Do not introduce React, Next.js, Tailwind, shadcn, a separate API framework, or another frontend stack unless explicitly requested.

## Core Rules

1. Read the relevant existing code before editing it.
2. Prefer supported Frappe APIs and conventions over custom infrastructure.
3. Keep business rules, validation, and authorization on the server.
4. Make the smallest coherent change that solves the requested problem.
5. Avoid unrelated refactors and formatting churn.
6. Preserve existing behavior/API contracts unless a change is intentional.
7. Never hardcode credentials, passwords, tokens, or secrets.
8. Do not silently change attendance rules, scheduler behavior, permissions, or data models during unrelated work.
9. Do not commit, push, merge, or use destructive Git commands unless explicitly requested.
10. Do not claim work is complete without fresh verification.

## Skill Routing

Use skills by task; do not load every skill every time.

* **Always for Frappe implementation:** `frappe-app-dev`
* **UI/UX:** `ui-design`, `frappe-impl-ui-components`, `web-design-guidelines`
* **Architecture/refactoring:** `frappe-agent-architect`, `improve-codebase-architecture`, optionally `codebase-design`, `domain-modeling`
* **DocType/database:** `frappe-syntax-doctypes`, `frappe-core-database`, `frappe-core-permissions`, optionally `domain-modeling`
* **API/backend:** `frappe-core-api`, `frappe-core-permissions`
* **Bug fixing:** `diagnosing-bugs`, `frappe-testing-unit`
* **Testing/final validation:** `frappe-testing-unit`, `frappe-agent-validator`, `verification-before-completion`

If a listed skill is unavailable, follow the equivalent rules in this file rather than blocking the task.

## Repository Map

```text
st_attendance_tracker/
├── api.py                  # Existing whitelisted API/application logic
├── tasks.py                # Scheduler jobs, reminders, reports
├── time_utils.py           # Shared time helpers
├── setup.py                # Setup/custom fields
├── hooks.py                # Hooks, scheduler, routes, assets
├── patches/                # Migration/backfill patches
├── public/css/
├── public/js/
├── st_attendance_tracker/doctype/
│   ├── daily_task/
│   ├── daily_task_log/
│   ├── employee_department_assignment/
│   ├── recurring_task_template/
│   ├── report_recipient/
│   └── st_attendance_settings/
└── www/
    ├── daily_checkin.*
    ├── team_dashboard.*
    ├── management_dashboard.*
    ├── my_history.*
    └── recurring_tasks.*
```

Before creating a new DocType, module, endpoint, helper, or page, search for an existing owner of that responsibility.

## Implementation Workflow

For non-trivial work:

1. Inspect relevant files and existing tests.
2. Trace the complete flow: **UI → API/controller → business rules → permissions → DocTypes/database → side effects**.
3. Establish expected behavior and edge cases before implementation.
4. For substantial work, create/update a spec in `specs/`; use `PROGRESS.md` for multi-phase work.
5. Use a **tracer bullet**: implement the smallest real end-to-end slice first.
6. Add/update tests while implementing.
7. Verify before reporting completion.

Do not generate large amounts of code from assumptions.

## Architecture

Organize code around real capabilities: attendance, tasks, recurring tasks, history, reporting, and permissions.

New `@frappe.whitelist()` methods should stay thin:

1. normalize/validate input,
2. identify the current user/Employee,
3. enforce authorization,
4. call business/service logic,
5. return a stable response.

Do not keep adding large workflows, repeated queries, email logic, and permission logic directly to `api.py`. As areas grow, extract cohesive modules such as `attendance/service.py`, `attendance/queries.py`, `attendance/permissions.py`, `task_management/service.py`, or `reporting/service.py`. Refactor incrementally; do not reorganize the whole repository just to match an ideal structure.

Do **not** force OOP. Use classes only when meaningful state, framework integration, or extensibility justifies them. Otherwise prefer well-named functions/modules. Function/file size is a signal, not a hard line-count limit.

## Frappe Development

* Prefer Frappe Document API, ORM, Query Builder, hooks, permissions, cache, and utilities.
* Use `frappe.get_doc`, `frappe.get_all`, `frappe.get_list`, `frappe.db.*`, or `frappe.qb` as appropriate.
* Avoid raw SQL for ordinary CRUD/query work. If truly needed, parameterize inputs, justify it, preserve permissions, and add tests.
* Do not bypass hooks/validation with direct DB writes unless deliberate and justified.
* Do not use `ignore_permissions=True` casually.
* Use supported Frappe v15 APIs; avoid undocumented internals when public APIs exist.
* Use standard Frappe exceptions/messages for user-facing failures where appropriate.

## DocType / Database Design

Before creating or changing a DocType, establish the business concept, ownership, cardinality, lifecycle, uniqueness, permissions, query/reporting needs, and migration/backfill requirements. Check whether Frappe/ERPNext already provides the concept first.

Prefer native relationships:

* `Link` for references
* `Table` / Child DocType for owned one-to-many data
* `Select` for small controlled vocabularies
* standard Date/Datetime/Time fields for temporal values

Avoid comma-separated relationships or JSON blobs when DocTypes/links are suitable. Use Section Breaks, Column Breaks, and Tab Breaks intentionally for usable Desk forms. Do not persist cheaply derived values unless required for audit history, reporting performance, or historical correctness.

For changes affecting existing installations, create an idempotent patch when migration/backfill is needed, register it in `patches.txt`, handle legacy data safely, and verify both existing and new records.

## Permissions / Security

Authorization is a server-side requirement. For every endpoint exposing employee/task data, determine whether the caller is the employee, the appropriate Team Leader, authorized HR/Management, or not allowed.

* Never trust client-provided employee identity, role, department, ownership, task IDs, or file ownership.
* Re-resolve authorization server-side.
* Hidden/disabled UI is not security.
* Apply least privilege.
* Protect attachment view/delete using the same ownership/manager rules as the parent task.
* Centralize repeated permission checks.
* Authorize before returning sensitive data or mutating documents.
* Do not expose internal errors, SQL, secrets, or unnecessary employee information.
* Permission changes require tests for both allowed and denied paths.

## API Design

For whitelisted methods:

* validate required values/types at the boundary,
* normalize JSON/string payloads once,
* use POST semantics for mutations where appropriate,
* return only fields the UI needs,
* avoid N+1 queries,
* keep response shapes stable,
* inspect all callers before changing an existing contract,
* extend an existing endpoint only when doing so keeps its purpose clear.

## UI / UX

Use Frappe-native design language and components. Preserve consistency with ERPNext/Frappe v15 and the existing app.

Every UI change should consider information hierarchy, desktop/mobile layouts, loading, empty, validation/error, disabled, success/confirmation states, keyboard usability, visible focus, semantic HTML, accessible labels, and sufficient contrast.

Avoid unnecessary animation, visual noise, and a second design system. Do not keep growing large HTML files with extensive inline CSS/JS. When touching a large page, extract cohesive styles/behavior to `public/css/` and `public/js/` when useful without turning a focused fix into a rewrite.

Destructive actions such as delete/reset require meaningful confirmation, server-side authorization, and clear success/error feedback.

## Attendance / Time

Attendance/time logic is business-critical.

* Prefer Frappe date/time utilities over ad-hoc string math.
* Treat the site timezone as authoritative unless a specific business rule states otherwise.
* Validate login/logout ordering, lunch ranges, half-day sessions, holidays, and missing values.
* Preserve historical correctness when settings such as late threshold or workday hours change.
* Add regression/edge-case tests when modifying attendance calculations.
* Do not change scheduler times as a side effect of unrelated work.

## Scheduler / Reports

Scheduler configuration lives in `hooks.py`; scheduled/report logic primarily lives in `tasks.py`.

When changing scheduled jobs, verify cron/timezone assumptions, make jobs safe to retry where practical, prevent duplicate notifications/reports, handle missing Employee/User/email data, avoid unbounded queries, and do not send real external email from automated tests.

## Testing

Every bug fix should include a regression test when reasonably automatable. New backend features should test core business behavior. Prioritize tests for morning check-in/EOD flows, attendance calculations, task rollover, recurring tasks, Team Leader vs HR/Management permissions, attachments, date/time edge cases, and migrations.

From the Bench directory:

```bash
bench --site excel run-tests --app st_attendance_tracker
```

If the active site is not `excel`, use the actual development site. Run narrow tests while iterating, then the relevant broader suite before completion.

Lint/format when relevant:

```bash
pre-commit run --all-files
```

Avoid unrelated formatting churn.

For UI changes, when browser tooling is available verify the happy path, validation/error path, narrow/mobile viewport, role/permission visibility, and obvious console errors.

Automated Playwright E2E is **not currently implemented in this repository**. Do not claim Playwright coverage exists unless it has actually been added.

## Bug-Fixing Process

1. Reproduce or establish the failure condition.
2. Trace the responsible layer.
3. Identify the root cause rather than patching only the symptom.
4. Add a failing regression test when feasible.
5. Implement the smallest correct fix.
6. Run the regression and relevant surrounding tests.
7. Check adjacent code for the same pattern without unnecessarily expanding scope.

Do not guess at root causes when repository evidence can answer the question.

## Specs / Planning

Use a tracer-bullet approach for substantial changes. Create a spec in `specs/` when work spans multiple layers, changes schema/permissions, introduces important business rules, or needs an architectural decision.

Suggested spec:

```text
# Feature / Change
## Goal
## User behavior
## Current behavior
## Constraints
## Data model impact
## Permission model
## API changes
## UI states
## Migration/backfill
## Test plan
## Risks
```

Use `PROGRESS.md` for multi-phase work that benefits from resumable progress tracking. Do not create planning files for trivial fixes.

## Code Style

* Prefer descriptive names over abbreviations.
* Keep control flow explicit and easy to follow.
* Reduce duplication only when a real shared concept exists.
* Avoid speculative abstractions.
* Comments explain **why**, constraints, or surprising behavior—not obvious code.
* Keep docstrings concise and useful.
* Match existing Python/JavaScript/Frappe conventions and repository lint configuration.
* Do not add dependencies when Frappe or the standard library already solves the problem adequately.

## Existing Domain Concepts

Custom DocTypes:

* `Daily Task`
* `Daily Task Log`
* `Employee Department Assignment`
* `Recurring Task Template`
* `Report Recipient`
* `ST Attendance Settings`

Main routes:

* `/daily-checkin`
* `/team-dashboard`
* `/management-dashboard`
* `/my-history`
* `/recurring-tasks`

The current access model distinguishes employee self-service, Team Leader access to relevant/direct-report employees, and HR/Management access to broader employee data. Preserve and centralize these rules instead of creating parallel authorization models.

## Before Declaring Completion

Use `verification-before-completion`, then:

1. Re-read the requested behavior.
2. Inspect the diff for accidental/unrelated changes.
3. Run relevant automated tests.
4. Run relevant lint/format checks.
5. Verify migrations for schema/data changes.
6. Test both authorized and unauthorized paths for permission changes.
7. Perform focused manual browser verification for UI changes when possible.
8. Confirm existing callers still match changed API contracts.
9. State exactly what was verified and what could not be verified.

Never say "fully working", "fixed", or "all tests pass" without fresh evidence.

## Completion Summary

When finishing implementation work, report concisely:

* **Changed** — what changed
* **Why** — important root-cause/design decision
* **Verified** — exact tests/checks run
* **Not verified** — anything that could not be tested, if applicable
* **Follow-up** — only genuine remaining work; omit if none
