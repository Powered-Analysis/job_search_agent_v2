# Title
#### tl;dr

A short 2-3 sentence summary explaining the problem being solved, core benefits, key features, and target audience. This should be specific enough that someone could explain the product after reading only this section.

------
#### Goals

##### Business Goals 
List 3-5 measurable objectives aligned with the company strategy. Each goal should have a clear metric. 
##### User Goals 
List 3-5 key benefits or outcomes for the end user. Focus on how the product solves a real user need. 
##### Non-Goals 
Identify 2-3 items explicitly out of scope to keep the project focused and prevent scope creep.

-----
#### User Stories

List user personas and their stories. For each persona, include 1-5 user stories in the format: As a {USER TYPE}, I want to {ACTION}, so that {BENEFIT}. Stories should cover the core happy path as well as important secondary flows. Group stories by persona.

-----
#### Functional Requirements

List features and functionality grouped by product area and priority: 
- Feature Group (Priority: P0/P1/P2) 
- Feature Name: Description of what it does and any key constraints 

P0 = must ship, P1 = should ship, P2 = nice to have. Be specific about behavior, not just naming features.

-----
#### User Experience

Describe the end-to-end UX flow that the AI should implement. 

**Entry Point & First-Time Experience** How users discover or access the product. Any onboarding steps or empty states. 

**Core Experience** Step-by-step UI flows with specific component choices: 
- Step 1: What the user does first. UI elements, validation, navigation. 
- Step 2: Next action or screen. Continue for each major step. 

**Edge Cases** Error states, empty states, loading states, and uncommon scenarios to handle.

-----
#### Technical Considerations

High-level technical factors that influence architecture decisions. Describe constraints, performance requirements, and integration needs. Call out any non-obvious technical decisions the AI should be aware of.

-----
#### Integration Points

List all third-party services, APIs, and integrations the product needs. For each, specify: what it does, which SDK/library to use, and any configuration needed. Include payment processors, email services, analytics, AI/ML APIs, and any other external dependencies.

---
#### Outstanding Questions

List all open questions or topics relevant to the product area that must be resolved in order to finalize the PRD. This can include decisions that are not finalized, questions that need answers from other team members, discrepancies between other PRDs with shared information, etc.