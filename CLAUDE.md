Implementation

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

The site is excel.localhost:8000 (Administrator/admin)