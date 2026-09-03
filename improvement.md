# UI/UX Improvement Plan

Based on a detailed audit of the **Daily Check-In** and **Additional Work** portal pages against the core design principles (minimalism, unified alignment, tight spacing, and avoiding bulky boxes), here are the specific improvements needed:

## 1. Daily Check-in Page (`/daily-checkin`)

### Alignment & Spacing Issues
* **Top Header Alignment:** The date pill (`2026-09-02`) and top action buttons (`Docs`, User Avatar) lack consistent vertical alignment and padding relative to the app title header.
* **Form Controls in Detailed View:** Inline form controls (`Carry forward`, `EST.`, `ACTUAL TIME`, `Add remark`, `Attach`) appear squeezed on medium viewport widths and need clearer vertical lane alignment. They currently drift rather than forming strict columns.
* **Textarea Inputs:** Task description textareas feature visible raw browser resize drag handles and default grey borders that break the minimalist aesthetic.
* **Concise View Table Header Alignment:** The table column headers (`#`, `TASK`, `EST.`, `ACTUAL`, `STATUS`, `ACTION`) sit above the group dividers, breaking visual vertical alignment with the tasks nested beneath the project headers.

### Color & Contrast Consistency
* **Status Pills & Alerts:** The `CHECKED IN` status pill uses high-contrast neon green, which clashes harshly against the soft indigo/violet color palette.
* **Alert integration:** The lunch break validation message appears as a raw alert banner floating between cards rather than a soft, integrated inline warning badge.
* **Bottom Action Bar:** The floating bottom action bar container is narrower than the main content area, creating a visual disconnect with the right summary sidebar.

---

## 2. Additional Work Page (`/additional-work`)

### Layout Asymmetry (The "Widescreen Void")
* **Missing Sidebar:** The page currently lacks a right summary sidebar (unlike Daily Check-in's 3-column layout), leaving a massive blank space on the right side of standard monitor resolutions.
* **Solution:** Upgrade to a 3-column layout by adding a right sidebar displaying an "Additional Work Log History" or "Monthly Summary" to unify design consistency across portal pages.

### Design Language Disconnect
* **Input Styling Inconsistency:** Standalone tasks display raw heavy rectangular outlines around textareas, whereas project tasks use soft, borderless inputs.
* **Date Selection Card:** The date selector relies on standard HTML date inputs and generic subtext without quick selection chips (e.g., "Today", "Yesterday").
* **Button Hierarchy:** Primary actions (`+ Add task` and `Submit additional work`) use stark solid black backgrounds, while `Add project` uses a light bordered style. These should follow a single cohesive color system (violet primary accent, subtle outlines for secondary actions).

### Missing Features
* **Lack of Concise View Toggle:** Additional Work only supports expanded card form inputs. Introducing the Detailed View / Concise View toggle here will streamline submitting multiple quick additional work logs and ensure consistency with the Check-In page.

## Proposed Next Steps
1. **Design Token Standardization:** Unify button themes, input fields, badge colors, and border radii across both pages via `st_tracker_shared.css`.
2. **Additional Work Layout Upgrade:** Redesign `/additional-work` into a 3-column layout with a right summary panel and view toggle.
3. **Refine Concise View:** Adjust table headers in the check-in concise view so columns perfectly align vertically down the page.
