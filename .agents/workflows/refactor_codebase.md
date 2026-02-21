---
description: Codebase refactoring for simplicity and readability
---

# Phase 1: Investigation and Planning

1. Use the `task_boundary` tool to start the task. Set `TaskName` to "Phase 1: Codebase Investigation and Refactoring Plan".
2. Investigate the entire codebase (`find_by_name`, `list_dir`, `view_file_outline`, `view_file`, `grep_search`) looking through the lens of a senior engineer.
3. Identify areas of complex logic, poor naming, lack of modularity, or confusing patterns.
4. Propose refactors that make the code simpler to read and maintain for a junior engineer. Focus strictly on simplicity over cleverness.
5. Create a TODO list of refactors in a plan document (e.g., `refactoring_plan.md`). For each item, explicitly describe how the change makes the code more readable.
6. For each proposed refactor, evaluate and document how it should be tested:
   - Unit testing.
   - Browser agent testing. **Important Requirement:** Always use `127.0.0.1` and NOT `localhost` for browser testing.
   - **Context Reminder:** Replicate the UX specified in `claude.md`. Keep in mind that the poller and WhatsApp integration are NOT yet complete.
7. Use the `notify_user` tool to request the user's review and approval of the proposed TODO list before proceeding.

# Phase 2: Implementation and Testing

1. Once approved, update the task boundary. Set `TaskName` to "Phase 2: Implementing Refactors".
2. Implement the approved TODO list items step by step. Use code-editing tools for modifications.
3. After each individual refactor step, perform regression testing using the designated approach (unit test or browser agent testing) to ensure existing functionality remains intact.
4. Mark items off the TODO list as you check them off.

# Phase 3: Post-Refactoring Review

1. Update the task boundary. Set `TaskName` to "Phase 3: Final Codebase Review".
2. Re-investigate the entire codebase to evaluate the success of the refactoring.
3. Ensure the overall architecture feels cleaner and junior-friendly, catching any remaining rough edges.
4. Notify the user when the final review is complete.
