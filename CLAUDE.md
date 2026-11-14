# Documentation Rules
## Code Quality and Verification
- Write clean, readable, and maintainable code with consistent naming and focused functions.
- Explicitly list any assumptions, missing requirements, unresolved questions, or implementation gaps.
- Add or update relevant unit tests and run them after making changes. Report the results and explain any tests that could not be run.
- Identify relevant edge cases and verify how the implementation handles them, adding test coverage where appropriate.
- At the end of each task, list all files created or modified and briefly describe each change.
- Include clear comments in every source file explaining its purpose, key functions, and overall execution or data flow. Keep comments accurate and focused on intent and non-obvious behavior.

## Project Flow Documentation
- Maintain `PROJECT_FLOW.md` with ONLY these 4 scannable sections (keep bullets under 12 words):
  1. **Architecture Map**: One ASCII flow diagram (`Client -> API -> Service -> DB`) + 5-row file-location table.
  2. **Core Write/Read Flows**: Numbered step-by-step bullets tracing data from UI click to SQL query and back.
  3. **Key Trade-offs & Guards**: 3-5 bullets on concurrency, validation, security, and edge cases handled.
  4. **Slice Changelog**: Newest-first 1-line summary of each completed slice + known limitations.
- Update `PROJECT_FLOW.md` at the end of every slice before asking me for the next step.


