# Personal Expense Tracker App

## Concept
An app where the user can upload a bank statement or photo of a receipt, and all expenses are automatically extracted, categorized, and added to a list. The goal is to always have a clear overview of where money is going and how much is available to invest.

**Target user:** Single user (personal use only)

---

## MVP Features
- User can photograph/upload a receipt — the app extracts the expense and categorizes it automatically
- User can upload a bank statement — all transactions are extracted, categorized, and cross-referenced with existing entries to avoid duplicates
- User can view a list of all expenses and filter them by category

---

## UI Screens

### Screen 1: Login Page
- Logo + app name at the top
- Two fields: Name and Password
- One button: "Log in"
- Login is hardcoded for a single user (no signup needed)
- Session resets after every logout — this page always appears first on launch

### Screen 2: Main Dashboard
**Left panel** — scrollable list of expense items:
- Each item shows: expense name + date of expense
- Each item has a dropdown chevron (expandable to show more details)
- Top of list: filter button (funnel icon) to filter expenses
- Bottom of each item: category dropdown selector to assign/change the category

**Right panel** — blank space reserved for future graphs/charts (empty for MVP)

**Top right corner** — profile icon → dropdown with "Log out" option

**Bottom right corner** — "+" button to add a new expense entry

### Screen 3: Add New Expense (modal/overlay)
- Triggered by clicking the "+" button
- Opens as a modal with a gray semi-transparent overlay behind it
- Inside the modal:
  - A/B toggle at the top: **Receipt** (default) | **Bank Statement**
  - "Open" button to open a file browser
  - Drag-and-drop zone with upload icon and "drag file here" label
- User picks input type, then drags a file in or opens it manually
